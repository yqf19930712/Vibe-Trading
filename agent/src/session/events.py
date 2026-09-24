"""SSE event bus with support for last_event_id recovery and buffering.

V5: Fixes the thread-safety issue caused by calling queue.put_nowait() on asyncio.Queue from a background thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from collections import deque

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Deque, Dict, List, Optional

# High-frequency incremental events. They are the only ones a full subscriber
# queue or the replay buffer may shed: losing a text delta costs a flicker in
# a live view, losing an ``llm_usage`` / ``attempt_stats`` / ``attempt.*``
# event costs the caller its billing or its terminal signal.
LOSSY_EVENT_TYPES = frozenset({"text_delta", "tool_progress", "elapsed_s", "heartbeat"})
# ``swarm.event`` wraps every worker event; only the per-token text stream is
# high-frequency, the lifecycle events inside it are kept.
_LOSSY_SWARM_EVENT_TYPES = frozenset({"worker_text"})
SUBSCRIBER_QUEUE_SIZE = 200
# ``llm_usage`` source of a swarm run that finished after its attempt stopped
# waiting (``src.tools.swarm_tool``); see ``EventBus._attempt_window``.
SWARM_TAIL_SOURCE = "swarm_tail"
# Published event ids are ``<epoch>-<seq>``: ``epoch`` is 8 hex chars drawn
# once per bus (i.e. per engine process), ``seq`` a decimal counter that
# strictly increases in publish order across every session of the bus. Within
# one epoch a larger seq is a later event, so a consumer can keep a
# high-water mark instead of a set of seen ids, and a reconnect can resume
# after an id that has already left the buffer.
_SEQ_ID_RE = re.compile(r"([0-9a-f]{8})-(\d+)")


def _is_swarm_tail_usage(event: "SSEEvent") -> bool:
    return event.event_type == "llm_usage" and event.data.get("source") == SWARM_TAIL_SOURCE


@dataclass
class SSEEvent:
    """Server-sent event.

    Attributes:
        event_id: Globally unique event ID used for last_event_id recovery;
            ``EventBus.publish`` replaces it with ``<epoch>-<seq>``.
        event_type: Event type stored in the SSE ``event`` field.
        data: Event payload.
        session_id: Owning session ID.
        timestamp: Event timestamp.
        seq: Publish order within the bus; set by ``EventBus.publish``.
    """

    event_id: Optional[str] = field(default_factory=lambda: uuid.uuid4().hex[:16])
    event_type: str = "message"
    data: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)
    seq: Optional[int] = None

    @property
    def lossy(self) -> bool:
        """Whether this event may be dropped under back-pressure."""
        if self.event_type in LOSSY_EVENT_TYPES:
            return True
        if self.event_type == "swarm.event":
            inner = self.data.get("event") if isinstance(self.data, dict) else None
            return isinstance(inner, dict) and inner.get("type") in _LOSSY_SWARM_EVENT_TYPES
        return False

    def to_sse(self) -> str:
        """Format the event as an SSE text frame.

        Returns:
            Text that conforms to the SSE specification.
        """
        payload = json.dumps(self.data, ensure_ascii=False)
        lines = []
        if self.event_id:
            lines.append(f"id: {self.event_id}")
        lines.extend([
            f"event: {self.event_type}",
            f"data: {payload}",
            "",
            "",
        ])
        return "\n".join(lines)


class _SubscriberQueue:
    """Delivery queue of one SSE subscriber.

    Bounded for stream deltas, lossless for everything else: when it is full
    a lossy event is dropped (or, for an incoming lossless event, the oldest
    queued lossy event makes room). A queue holding only lossless events
    grows past the bound — those are a handful per attempt. Only touched from
    the event-loop thread (``publish`` hands off with ``call_soon_threadsafe``)
    or synchronously when no loop runs, so it needs no lock of its own.
    """

    def __init__(self, maxsize: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._items: Deque[SSEEvent] = deque()
        self._maxsize = maxsize
        self._ready = asyncio.Event()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    def put(self, event: SSEEvent) -> bool:
        """Enqueue ``event``; returns False when it was dropped."""
        if len(self._items) >= self._maxsize:
            if event.lossy:
                self.dropped += 1
                return False
            for index, queued in enumerate(self._items):
                if queued.lossy:
                    del self._items[index]
                    self.dropped += 1
                    break
        self._items.append(event)
        self._ready.set()
        return True

    async def get(self, timeout: float) -> SSEEvent:
        """Return the next event, raising ``asyncio.TimeoutError`` after ``timeout``."""
        while not self._items:
            self._ready.clear()
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        return self._items.popleft()


class EventBus:
    """Session-scoped event bus with subscribers and buffered events.

    V5: Inject the asyncio event loop with ``set_loop()``, and use
    ``call_soon_threadsafe`` in ``publish()`` to preserve thread safety.

    Attributes:
        max_buffer_size: Maximum number of buffered events per session.
    """

    def __init__(self, max_buffer_size: int = 500) -> None:
        """Initialize the event bus.

        Args:
            max_buffer_size: Maximum number of buffered events per session.
        """
        self.max_buffer_size = max_buffer_size
        self._buffers: Dict[str, List[SSEEvent]] = {}
        self._subscribers: Dict[str, List[_SubscriberQueue]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._epoch = uuid.uuid4().hex[:8]
        self._seq = 0

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio event loop, usually during api_server startup.

        Args:
            loop: asyncio event loop.
        """
        self._loop = loop

    def publish(self, event: SSEEvent) -> None:
        """Publish an event to a session channel in a thread-safe way.

        Stamps the event with the next sequence number and its
        ``<epoch>-<seq>`` id. Subscriber hand-off happens under the same lock
        as the numbering, so every subscriber receives events in id order
        even when several threads publish at once — a consumer that keeps a
        high-water mark would otherwise drop the one that lost the race.

        Args:
            event: Event to publish.
        """
        session_id = event.session_id
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            event.event_id = f"{self._epoch}-{self._seq}"
            if session_id not in self._buffers:
                self._buffers[session_id] = []
            buffer = self._buffers[session_id]
            buffer.append(event)
            if len(buffer) > self.max_buffer_size:
                self._trim(buffer)

            # Safely enqueue onto the queue from inside the asyncio loop;
            # callbacks scheduled from here run in scheduling (= id) order.
            for queue in self._subscribers.get(session_id, []):
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._safe_put, queue, event)
                else:
                    queue.put(event)

    def _own_seq(self, event_id: Optional[str]) -> Optional[int]:
        """Sequence number of an id this bus issued, else None."""
        match = _SEQ_ID_RE.fullmatch(event_id or "")
        if match is None or match.group(1) != self._epoch:
            return None
        return int(match.group(2))

    def _trim(self, buffer: List[SSEEvent]) -> None:
        """Shrink ``buffer`` to ``max_buffer_size``, shedding lossy events first.

        A long streamed answer is hundreds of ``text_delta`` events; evicting
        strictly oldest-first pushed the attempt's ``attempt.created`` anchor,
        its ``llm_usage`` and ``attempt_stats`` out of the replay window.
        """
        excess = len(buffer) - self.max_buffer_size
        if excess <= 0:
            return
        keep: List[SSEEvent] = []
        for event in buffer:
            if excess > 0 and event.lossy:
                excess -= 1
                continue
            keep.append(event)
        if excess > 0:
            keep = keep[excess:]
        buffer[:] = keep

    @staticmethod
    def _safe_put(queue: _SubscriberQueue, event: SSEEvent) -> None:
        """Deliver an event to one subscriber queue (event-loop thread).

        Args:
            queue: Subscriber queue.
            event: SSE event.
        """
        if not queue.put(event):
            logger.warning("EventBus queue full, dropping %s event for session %s", event.event_type, event.session_id)

    def emit(
        self,
        session_id: str,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> SSEEvent:
        """Build and publish an event in one step.

        Args:
            session_id: Session ID.
            event_type: Event type.
            data: Event payload.

        Returns:
            The published SSEEvent.
        """
        event = SSEEvent(
            event_type=event_type,
            data=data or {},
            session_id=session_id,
        )
        self.publish(event)
        return event

    def replay(
        self,
        session_id: str,
        last_event_id: Optional[str] = None,
        *,
        replay_all: bool = False,
        since_attempt: Optional[str] = None,
    ) -> List[SSEEvent]:
        """Replay buffered session events for reconnect recovery.

        Args:
            session_id: Session ID.
            last_event_id: Last event ID received by the client.
            replay_all: Return the buffered stream from the beginning when
                ``last_event_id`` is absent. Used only for active run recovery;
                completed history is loaded through REST.
            since_attempt: With ``replay_all``, start the stream at this
                attempt's ``attempt.created`` event and leave out events
                stamped with another attempt id. The buffer is per session:
                without this a follow-up question replayed the previous
                attempt's ``llm_usage`` / ``attempt_stats`` to a caller that
                bills whatever arrives on the stream.

        A ``last_event_id`` this bus issued resumes right after it, whether
        or not that event is still buffered. Any other id (a relayed frame,
        a previous engine process) is looked up in the buffer; not found,
        ``replay_all`` replays the whole window.

        Returns:
            List of events that should be replayed.
        """
        with self._lock:
            return self._replay_locked(session_id, last_event_id, replay_all, since_attempt)

    def _replay_locked(
        self,
        session_id: str,
        last_event_id: Optional[str],
        replay_all: bool,
        since_attempt: Optional[str],
    ) -> List[SSEEvent]:
        """``replay`` body; the caller holds ``self._lock``."""
        buffer = self._buffers.get(session_id, [])
        if replay_all and since_attempt:
            buffer = self._attempt_window(buffer, since_attempt)
        if not last_event_id:
            return list(buffer) if replay_all else []  # First connect: history loaded via REST by default.
        last_seq = self._own_seq(last_event_id)
        if last_seq is not None:
            return [e for e in buffer if e.seq is not None and e.seq > last_seq]
        found = False
        result: List[SSEEvent] = []
        for event in buffer:
            if found:
                result.append(event)
            elif event.event_id == last_event_id:
                found = True
        if not found and replay_all:
            return list(buffer)
        return result

    @staticmethod
    def _attempt_window(buffer: List[SSEEvent], attempt_id: str) -> List[SSEEvent]:
        """Events of ``attempt_id``: from its ``attempt.created`` on, foreign attempts excluded.

        One foreign event is kept inside the window: a swarm tail's
        ``llm_usage`` (``source="swarm_tail"``). It carries the id of the
        attempt that stopped waiting for the run and is billed to the
        request streaming when it is published — this one, when it comes
        after the anchor.

        When the anchor itself has left the buffer, every remaining event is
        newer than it (lossless events are only evicted oldest-first, after
        every lossy one), so the attempt-id filter plus the swarm-tail
        exception is enough.
        """
        start = 0
        for index, event in enumerate(buffer):
            if event.event_type == "attempt.created" and event.data.get("attempt_id") == attempt_id:
                start = index
                break
        else:
            return [
                e for e in buffer
                if e.data.get("attempt_id") == attempt_id or _is_swarm_tail_usage(e)
            ]
        return [
            e for e in buffer[start:]
            if e.data.get("attempt_id") in (None, "", attempt_id) or _is_swarm_tail_usage(e)
        ]

    async def subscribe(
        self,
        session_id: str,
        last_event_id: Optional[str] = None,
        *,
        replay_all: bool = False,
        since_attempt: Optional[str] = None,
    ) -> AsyncIterator[SSEEvent]:
        """Subscribe to a session event stream asynchronously.

        Args:
            session_id: Session ID.
            last_event_id: Last event ID received by the client for reconnect recovery.
            replay_all: Replay all buffered events when no last event ID is
                available. This is opt-in for active run hydration.
            since_attempt: Limit that replay to one attempt (see ``replay``).

        Yields:
            SSEEvent objects.
        """
        queue = _SubscriberQueue()

        # Registering and taking the replay snapshot under one lock: every
        # event is either in the replay or in the queue, never both, and the
        # stream stays in id order.
        with self._lock:
            if session_id not in self._subscribers:
                self._subscribers[session_id] = []
            self._subscribers[session_id].append(queue)
            replay_events = self._replay_locked(
                session_id, last_event_id, replay_all, since_attempt
            )

        try:
            for event in replay_events:
                yield event

            while True:
                try:
                    event = await queue.get(timeout=30.0)
                    yield event
                except asyncio.TimeoutError:
                    yield SSEEvent(
                        event_id=None,
                        event_type="heartbeat",
                        data={"ts": time.time()},
                        session_id=session_id,
                    )
        finally:
            with self._lock:
                subs = self._subscribers.get(session_id, [])
                if queue in subs:
                    subs.remove(queue)

    def clear(self, session_id: str) -> None:
        """Clear the buffered events for a session.

        Args:
            session_id: Session ID.
        """
        with self._lock:
            self._buffers.pop(session_id, None)
