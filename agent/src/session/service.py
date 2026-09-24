"""Session lifecycle orchestration for message flow, attempt creation, and execution scheduling.

V5: Uses AgentLoop instead of the fixed pipeline behind the generate skill.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

# Dedicated thread pool limited to four concurrent agents to avoid exhausting the default executor.
_AGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")

logger = logging.getLogger(__name__)

# Strong references for fire-and-forget tasks: the event loop only keeps
# weak ones, so an attempt task with no other referrer could be collected
# mid-flight.
_bg_tasks: Set["asyncio.Task[Any]"] = set()


def _spawn(coro: Coroutine[Any, Any, Any]) -> "asyncio.Task[Any]":
    """Schedule ``coro`` as a task that stays referenced until it finishes."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task

from src.session import tombstone
from src.session.events import EventBus
from src.session.models import (
    Attempt,
    AttemptStatus,
    Message,
    Session,
)
from src.session.search import get_shared_index
from src.session.store import SessionStore


class SessionService:
    """Session lifecycle service.

    Attributes:
        store: Session persistence store.
        event_bus: SSE event bus.
        runs_dir: Root runs directory.
    """

    def __init__(
        self,
        store: SessionStore,
        event_bus: EventBus,
        runs_dir: Path,
    ) -> None:
        """Initialize the session service.

        Args:
            store: Session persistence store.
            event_bus: SSE event bus.
            runs_dir: Root runs directory.
        """
        self.store = store
        self.event_bus = event_bus
        self.runs_dir = runs_dir
        self._active_loops: Dict[str, "AgentLoop"] = {}
        # In-flight bookkeeping is per ATTEMPT: a session can briefly have
        # two (a cancelled one still finishing while the retry builds its
        # registry), and the older one's cleanup must not clear the newer
        # one's entries. ``_inflight`` maps session -> its live attempt ids;
        # ``_pending_cancel`` holds attempt ids whose cancel arrived before
        # their loop existed (delivered the moment the loop registers).
        self._inflight: Dict[str, Set[str]] = {}
        self._pending_cancel: Set[str] = set()
        self._attempt_loops: Dict[str, "AgentLoop"] = {}
        # Receipts that could not be persisted (full disk): kept in memory
        # and merged into get_messages so the caller polling for the answer
        # still gets it instead of waiting out its budget.
        self._volatile_receipts: Dict[str, List[Message]] = {}
        # Extra per-session cleanups run on delete (e.g. the goal ledger,
        # owned by the API layer), including the late re-sweep after a
        # deleted session's attempt finally exits.
        self._purge_hooks: List[Callable[[str], None]] = []
        self._search_index = get_shared_index()

    def add_purge_hook(self, hook: Callable[[str], None]) -> None:
        """Register a cleanup run whenever a session is purged."""
        self._purge_hooks.append(hook)

    def create_session(self, title: str = "", config: Optional[Dict[str, Any]] = None) -> Session:
        """Create a new session.

        Args:
            title: Session title.
            config: Session configuration.

        Returns:
            The newly created Session.
        """
        session = Session(title=title, config=config or {})
        self.store.create_session(session)
        self._search_index.index_session(session.session_id, title)
        self.event_bus.emit(session.session_id, "session.created", {"session_id": session.session_id, "title": title})
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Return a session by ID."""
        return self.store.get_session(session_id)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        """List all sessions."""
        return self.store.list_sessions(limit)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session: cancel its live loop, drop files, runs, events and FTS rows.

        Everything the session produced goes with it: the session directory,
        every ``runs/<id>`` whose ``req.json`` names the session (the run
        directories are the only other place the request lands), and the
        ``sessions.db`` rows (otherwise ``session_search`` keeps returning
        the deleted conversation as a snippet). Goal-ledger rows are the
        caller's job (``api_server`` owns the GoalStore).
        """
        # Tombstone first: an attempt of this session that is still running
        # finishes at its next cancel check, and every write it makes until
        # then must be refused rather than recreate what is deleted below.
        tombstone.mark(session_id, self.store.base_dir)
        self.cancel_current(session_id)
        deleted = self.store.delete_session(session_id)
        self._purge_session(session_id)
        return deleted

    def _purge_session(self, session_id: str, run_dir: Optional[str] = None) -> None:
        """Remove everything a deleted session left outside its directory.

        Called by ``delete_session`` and again when a deleted session's
        attempt finally exits, to sweep what that attempt wrote in between
        (trace, tool-result offloads into its run dir, swarm artifacts).
        """
        self.event_bus.clear(session_id)
        self._volatile_receipts.pop(session_id, None)
        self._delete_session_runs(session_id)
        if run_dir:
            self._delete_run_dir(Path(run_dir))
        self._delete_session_swarm_runs(session_id)
        try:
            self._search_index.delete_session(session_id)
        except Exception as exc:  # noqa: BLE001 - index cleanup is best-effort
            logger.warning("search index cleanup failed for session %s: %s", session_id, exc)
        try:
            from src.tools.background_tools import get_background_manager

            get_background_manager().cancel_session(session_id)
        except Exception:  # noqa: BLE001
            logger.debug("background task cleanup failed for %s", session_id, exc_info=True)
        for hook in list(self._purge_hooks):
            try:
                hook(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("session purge hook failed for %s: %s", session_id, exc)

    def _delete_run_dir(self, run_dir: Path) -> None:
        """Remove one run directory, only when it sits directly under ``runs_dir``."""
        import shutil

        try:
            if run_dir.is_symlink() or run_dir.resolve().parent != self.runs_dir.resolve():
                return
        except OSError:
            return
        shutil.rmtree(run_dir, ignore_errors=True)

    def _delete_session_swarm_runs(self, session_id: str) -> list[str]:
        """Remove the swarm run directories stamped with ``session_id``."""
        try:
            from src.swarm.store import delete_session_runs

            removed = delete_session_runs(session_id)
        except Exception as exc:  # noqa: BLE001 - best effort, like the other stores
            logger.warning("swarm run cleanup failed for session %s: %s", session_id, exc)
            return []
        if removed:
            logger.info("removed %d swarm run(s) of session %s", len(removed), session_id)
        return removed

    def _delete_session_runs(self, session_id: str) -> int:
        """Remove the run directories linked to ``session_id``; returns the count."""
        import shutil

        from src.core.state import runs_for_session

        removed = 0
        try:
            candidates = runs_for_session(self.runs_dir, session_id)
        except OSError as exc:
            logger.warning("run lookup failed for session %s: %s", session_id, exc)
            return 0
        for run_dir in candidates:
            shutil.rmtree(run_dir, ignore_errors=True)
            if not run_dir.exists():
                removed += 1
        return removed

    def reconcile_orphans(self, goal_store: Optional[Any] = None) -> list[str]:
        """Drop index/ledger rows of sessions whose directory is gone.

        Runs once at engine start. A session can be deleted from the host
        while this engine is not running (router offline delete): the
        directory disappears but its ``sessions.db`` rows — FTS messages,
        goal ledger — stay. This binds the search index to the store (so a
        later ``search()`` also drops orphans on sight), sweeps the FTS rows,
        and purges the goal ledger of every orphan session.

        Args:
            goal_store: ``GoalStore`` to purge alongside (optional).

        Returns:
            Session ids removed from the search index.
        """
        try:
            tombstone.prune(self.store.base_dir)
        except Exception:  # noqa: BLE001
            logger.debug("tombstone prune failed", exc_info=True)
        try:
            self._search_index.bind_store(self.store.base_dir)
            removed = list(self._search_index.reconcile_with_store())
        except Exception as exc:  # noqa: BLE001 - startup tidy-up must not block serving
            logger.warning("session index reconcile failed: %s", exc)
            removed = []
        if goal_store is not None:
            try:
                for sid in goal_store.list_session_ids():
                    if not (self.store.base_dir / sid).is_dir():
                        goal_store.delete_session_goals(sid)
                        if sid not in removed:
                            removed.append(sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("goal ledger reconcile failed: %s", exc)
        if removed:
            logger.info("dropped %d orphan session(s) from sessions.db", len(removed))
        return removed

    async def send_message(
        self,
        session_id: str,
        content: str,
        role: str = "user",
        *,
        include_shell_tools: bool = False,
        deadline_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Send a message to a session and trigger execution.

        Args:
            session_id: Session ID.
            content: Message content.
            role: Message role.
            include_shell_tools: Whether this attempt may use shell tools.

        Returns:
            Dictionary containing message_id and attempt_id.
        """
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        message = Message(session_id=session_id, role=role, content=content)
        self.store.append_message(message)
        self._search_index.index_message(session_id, role, content)
        self.event_bus.emit(session_id, "message.received", {"message_id": message.message_id, "role": role, "content": content})

        if role != "user":
            return {"message_id": message.message_id}

        attempt = Attempt(session_id=session_id, parent_attempt_id=session.last_attempt_id, prompt=content)
        self.store.create_attempt(attempt)
        session.config["include_shell_tools"] = include_shell_tools
        session.last_attempt_id = attempt.attempt_id
        session.updated_at = datetime.now().isoformat()
        self.store.update_session(session)
        self.event_bus.emit(session_id, "attempt.created", {"attempt_id": attempt.attempt_id, "prompt": content})

        self._inflight.setdefault(session_id, set()).add(attempt.attempt_id)
        _spawn(
            self._run_attempt(
                session,
                attempt,
                include_shell_tools=include_shell_tools,
                deadline_s=deadline_s,
            )
        )
        return {"message_id": message.message_id, "attempt_id": attempt.attempt_id}

    def get_messages(self, session_id: str, limit: int = 100) -> list[Message]:
        """Return the message history (plus any receipt the disk refused)."""
        messages = self.store.get_messages(session_id, limit)
        volatile = self._volatile_receipts.get(session_id)
        if volatile:
            stored = {m.message_id for m in messages}
            messages = messages + [m for m in volatile if m.message_id not in stored]
            messages = messages[-limit:]
        return messages

    def cancel_current(self, session_id: str) -> bool:
        """Cancel the currently running AgentLoop for a session.

        Args:
            session_id: Session ID.

        Returns:
            Whether anything received a cancel signal: a live loop, an attempt
            still in its preparation phase (cancelled as soon as its loop
            registers), or a swarm run this session left running.
        """
        cancelled = False
        loop = self._active_loops.get(session_id)
        if loop is not None:
            loop.cancel()
            cancelled = True
        # Every live attempt of the session, not only the newest loop: a
        # retry still building its registry has no loop yet and gets the
        # cancel delivered when it registers.
        for attempt_id in list(self._inflight.get(session_id, ())):
            attempt_loop = self._attempt_loops.get(attempt_id)
            if attempt_loop is not None:
                attempt_loop.cancel()
            else:
                self._pending_cancel.add(attempt_id)
            cancelled = True
        # A cancelled attempt has no one left to resume a swarm run it was
        # waiting on (or had handed back as wait_budget_exhausted), so the
        # run is stopped with it instead of burning on in the background.
        from src.swarm.runtime import cancel_session_runs

        runs = cancel_session_runs(session_id)
        if runs:
            logger.info("cancelled %d swarm run(s) of session %s: %s", len(runs), session_id, runs)
        return cancelled or bool(runs)

    async def _run_attempt(
        self,
        session: Session,
        attempt: Attempt,
        *,
        include_shell_tools: bool = False,
        deadline_s: Optional[float] = None,
    ) -> None:
        """Execute an Attempt in the background."""
        session_id = session.session_id
        self._inflight.setdefault(session_id, set()).add(attempt.attempt_id)
        try:
            await self._run_attempt_inner(
                session, attempt, include_shell_tools=include_shell_tools, deadline_s=deadline_s
            )
        finally:
            live = self._inflight.get(session_id)
            if live is not None:
                live.discard(attempt.attempt_id)
                if not live:
                    self._inflight.pop(session_id, None)
            self._pending_cancel.discard(attempt.attempt_id)
            self._attempt_loops.pop(attempt.attempt_id, None)
            # The session was deleted while this attempt ran: whatever it
            # wrote after the delete (trace, offloads, swarm artifacts) is
            # swept now that it can write no more.
            if self.store.is_deleted(session_id):
                self.store.delete_session(session_id)
                self._purge_session(session_id, run_dir=attempt.run_dir)

    async def _run_attempt_inner(
        self,
        session: Session,
        attempt: Attempt,
        *,
        include_shell_tools: bool,
        deadline_s: Optional[float],
    ) -> None:
        receipt_written = False
        try:
            attempt.mark_running()
            self.store.update_attempt(attempt)
            self.event_bus.emit(session.session_id, "attempt.started", {"attempt_id": attempt.attempt_id})

            messages = self.store.get_messages(session.session_id)
            result = await self._run_with_agent(
                attempt,
                messages=messages,
                include_shell_tools=include_shell_tools,
                session_config=dict(session.config),
                deadline_s=deadline_s,
            )
            answer = result.get("content") or ""
            if result.get("status") == "success" and answer.strip():
                attempt.mark_completed(summary=answer)
            elif result.get("status") == "success":
                # A run that left artifacts (e.g. metrics.csv) but no text is
                # not an answer: reporting it as success handed the caller a
                # canned "completed" line in place of the research result.
                attempt.mark_failed(error=self._empty_answer_reason(result))
            else:
                attempt.mark_failed(error=result.get("reason", "unknown"))
            attempt.run_dir = result.get("run_dir")

            # Bookkeeping writes on the way out are best effort: a full disk
            # must not turn an answer that exists into a failed attempt.
            try:
                self.store.update_attempt(attempt)
            except OSError as exc:
                logger.warning("attempt %s: update_attempt failed: %s", attempt.attempt_id, exc)
            reply_metadata = {}
            if attempt.run_dir:
                reply_metadata["run_id"] = Path(attempt.run_dir).name
            reply_metadata["status"] = attempt.status.value
            # Machine-readable outcome: without the flag the router would take
            # ANY non-empty assistant message linked to the attempt as the
            # answer, forwarding "Execution failed: …" prose to the user as if
            # it were the research result. Keep the prose, add the flag; the
            # router keys on it.
            reply_metadata["ok"] = attempt.status == AttemptStatus.COMPLETED
            if attempt.status != AttemptStatus.COMPLETED:
                reply_metadata["error"] = attempt.error or "unknown error"
            if attempt.metrics:
                reply_metadata["metrics"] = attempt.metrics

            reply = Message(
                session_id=session.session_id, role="assistant",
                content=self._format_result_message(attempt),
                linked_attempt_id=attempt.attempt_id,
                metadata=reply_metadata,
            )
            self._store_receipt(reply)
            receipt_written = True
            try:
                self._search_index.index_message(session.session_id, "assistant", reply.content)
            except Exception:  # noqa: BLE001 - the answer is delivered; search is secondary
                logger.warning("attempt %s: receipt not indexed", attempt.attempt_id, exc_info=True)
            self.event_bus.emit(
                session.session_id,
                "attempt.completed" if attempt.status == AttemptStatus.COMPLETED else "attempt.failed",
                {"attempt_id": attempt.attempt_id, "status": attempt.status.value,
                 "summary": attempt.summary, "error": attempt.error, "run_dir": attempt.run_dir},
            )

        except Exception as exc:
            self._fail_attempt(session, attempt, exc, receipt_written=receipt_written)

    def _store_receipt(self, reply: Message) -> None:
        """Persist the assistant receipt; keep it in memory when the disk refuses.

        The router finds the answer by polling the message list, so a receipt
        lost to a full disk would leave it waiting out the whole budget for an
        answer that was already produced.
        """
        if self.store.is_deleted(reply.session_id):
            return
        try:
            self.store.append_message(reply)
        except OSError as exc:
            logger.warning(
                "attempt %s: receipt not persisted (%s); serving it from memory",
                reply.linked_attempt_id, exc,
            )
            held = self._volatile_receipts.setdefault(reply.session_id, [])
            held.append(reply)
            del held[:-5]

    @staticmethod
    def _empty_answer_reason(result: Dict[str, Any]) -> str:
        """Failure reason for a run the loop called successful but that has no text."""
        iterations = result.get("iterations")
        max_iterations = result.get("max_iterations")
        if iterations and max_iterations and iterations >= max_iterations:
            return (
                f"reached max iterations ({max_iterations}) without a final answer "
                "(run artifacts exist but the model produced no text)"
            )
        return "no final answer: the run produced artifacts but the model returned no text"

    def _fail_attempt(
        self, session: Session, attempt: Attempt, exc: Exception, *, receipt_written: bool
    ) -> None:
        """Terminal bookkeeping for an attempt that raised outside the loop.

        Writes the same ``ok=false`` assistant receipt the in-loop failure
        path writes, so a consumer polling the message list (the router) sees
        the failure at once instead of waiting out its whole budget. Each
        step is independent: a store that is failing (full disk) must not
        stop the event from going out, and vice versa.
        """
        logger.exception(
            "attempt %s of session %s failed outside the agent loop: %s",
            attempt.attempt_id, session.session_id, exc,
        )
        error = str(exc) or type(exc).__name__
        attempt.mark_failed(error=error)
        try:
            self.store.update_attempt(attempt)
        except Exception:  # noqa: BLE001 - keep going, the receipt matters more
            logger.warning("attempt %s: update_attempt failed", attempt.attempt_id, exc_info=True)
        if not receipt_written:
            reply = Message(
                session_id=session.session_id, role="assistant",
                content=self._format_result_message(attempt),
                linked_attempt_id=attempt.attempt_id,
                metadata={"status": attempt.status.value, "ok": False, "error": error},
            )
            try:
                self._store_receipt(reply)
            except Exception:  # noqa: BLE001
                logger.warning("attempt %s: failure receipt not stored", attempt.attempt_id, exc_info=True)
            try:
                self._search_index.index_message(session.session_id, "assistant", reply.content)
            except Exception:  # noqa: BLE001
                logger.debug("attempt %s: receipt not indexed", attempt.attempt_id, exc_info=True)
        try:
            self.event_bus.emit(
                session.session_id, "attempt.failed",
                {"attempt_id": attempt.attempt_id, "status": attempt.status.value,
                 "error": error, "run_dir": attempt.run_dir},
            )
        except Exception:  # noqa: BLE001
            logger.warning("attempt %s: attempt.failed event not emitted", attempt.attempt_id, exc_info=True)

    async def _run_with_agent(
        self,
        attempt: Attempt,
        messages: list = None,
        *,
        include_shell_tools: bool = False,
        session_config: Optional[Dict[str, Any]] = None,
        deadline_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute an attempt with the V5 AgentLoop.

        Args:
            attempt: Current execution attempt.
            messages: Session message history.
            include_shell_tools: Whether the registry may include shell tools.
            session_config: Optional session-level config overrides. MCP server
                definitions under the ``mcpServers`` key are merged on top of
                the user config file via ``load_runtime_agent_config`` so each
                session can extend or override the global MCP server list.

        Returns:
            Result dictionary containing status, run_dir, run_id, metrics, and related fields.
        """
        import contextvars
        import os
        import time

        from src.tools import build_registry
        from src.providers.chat import ChatLLM
        from src.agent.loop import AgentLoop
        from src.memory.persistent import PersistentMemory
        from src.config.loader import load_runtime_agent_config, sanitize_session_overrides
        from src.core.budget import bind_deadline
        from src.core.logging_setup import bind_log_context

        llm = ChatLLM()
        pm = PersistentMemory()

        session_id = attempt.session_id
        attempt_id = attempt.attempt_id
        loop = asyncio.get_running_loop()

        # Correlate every log line of this attempt (loop, tools, data loaders)
        # with the router ask log / laicai deep_engine_runs via attempt_id.
        # Executor threads don't inherit contextvars, so run the sync work
        # inside an explicit context copy.
        bind_log_context(session_id=session_id, attempt_id=attempt_id)
        # Bind the wall-clock budget so the loop (and every tool thread it
        # spawns via copy_context) can finalize before the caller's timeout.
        deadline = time.monotonic() + deadline_s if deadline_s else None
        bind_deadline(deadline)

        safe_overrides = sanitize_session_overrides(session_config) if session_config else session_config
        agent_config = load_runtime_agent_config(overrides=safe_overrides)

        def event_callback(event_type: str, data: Dict[str, Any]) -> None:
            """Forward AgentLoop events to the SSE event bus."""
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        def _mcp_collision_warn(msg: str) -> None:
            """Forward MCP server-name collision warnings to the operator event channel."""
            self.event_bus.emit(session_id, "mcp.warning", {"attempt_id": attempt_id, "message": msg})

        registry = await loop.run_in_executor(
            _AGENT_EXECUTOR,
            contextvars.copy_context().run,
            lambda: build_registry(
                persistent_memory=pm,
                include_shell_tools=include_shell_tools,
                agent_config=agent_config,
                session_id=session_id,
                event_callback=event_callback,
                warn_callback=_mcp_collision_warn,
            ),
        )

        # Iteration ceiling is an env knob: the multi-tenant router hands
        # laicai tenants their tier (50, see ops/cube-router engine_env);
        # wall-clock deadlines, not the iteration count, are the hard stop.
        try:
            max_iterations = max(1, int(os.getenv("VIBE_MAX_ITERATIONS", "50")))
        except ValueError:
            max_iterations = 50
        agent = AgentLoop(
            registry=registry,
            llm=llm,
            event_callback=event_callback,
            max_iterations=max_iterations,
            persistent_memory=pm,
        )
        # A second attempt on the same session must not silently overwrite
        # the registry entry: that orphans the first loop — unreachable by
        # cancel_current, still burning tokens.
        previous = self._active_loops.get(session_id)
        if previous is not None and previous is not agent:
            previous.cancel()
        self._active_loops[session_id] = agent
        self._attempt_loops[attempt_id] = agent
        # A cancel that arrived during build_registry had no loop to hit;
        # deliver it now, before the loop is even scheduled.
        if attempt_id in self._pending_cancel:
            self._pending_cancel.discard(attempt_id)
            agent.cancel()

        # Build the message history context.
        history = (
            self._convert_messages_to_history(messages, session_id=session_id)
            if messages
            else None
        )

        try:
            result = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                contextvars.copy_context().run,
                lambda: agent.run(
                    user_message=attempt.prompt,
                    history=history,
                    session_id=session_id,
                ),
            )
        finally:
            # Only drop OUR entry — a newer loop may have replaced it.
            if self._active_loops.get(session_id) is agent:
                self._active_loops.pop(session_id, None)
            self._attempt_loops.pop(attempt_id, None)

        # Load metrics from the run output when available.
        if result.get("run_dir"):
            metrics = self._load_metrics(Path(result["run_dir"]))
            if metrics:
                result["metrics"] = metrics

        return result

    @staticmethod
    def _convert_messages_to_history(
        messages: list,
        session_id: str = "",
    ) -> list[Dict[str, Any]]:
        """Convert Session messages into OpenAI-format history.

        Keeps the readable ``[prev_run: {run_id}]`` marker instead of removing it
        completely, and trims by budget instead of a hard six-message cap so the
        LLM can still see previous artifact paths and strategy content during
        iterative updates.

        Two layers:

        1. The session's stored handoff summary (produced by Layer 3 of a
           previous attempt) is prepended as background reference. Without it,
           everything an earlier attempt compressed away was simply gone: the
           replay only ever carried raw user/assistant text.
        2. Raw turns are then filled newest-first against a TOKEN budget using
           the CJK-weighted estimator, not a flat character count. The old
           ``MAX_HISTORY_CHARS = 12000`` was annotated "roughly 3000 tokens",
           which holds for English only — by this repo's own estimator 12k
           Chinese characters is ~7.2k tokens, so the two co-existing units
           disagreed by 2.4x. ``MAX_HISTORY_TOKENS`` is deliberately set to
           6000 (≈ today's real Chinese-session volume) rather than the 3000 of
           the stale comment: this change unifies the unit, it does not also
           halve the budget.

        Args:
            messages: Session message list without the current turn.
            session_id: Session id, used to load the handoff summary. Empty =
                no summary layer (the raw-replay behavior).

        Returns:
            OpenAI-format messages: optional summary + omission note + the
            newest raw turns that fit the token budget.
        """
        import re
        from pathlib import Path

        from src.core.token_estimate import estimate_text_tokens
        from src.session import handoff
        from src.agent.context_policy import HANDOFF_PREFIX

        MAX_HISTORY_TOKENS = 6_000
        HANDOFF_INJECT_MAX_TOKENS = 2_000
        # Rough per-message envelope overhead (role, delimiters).
        PER_MESSAGE_TOKENS = 8

        def _shorten_run_dir(match: re.Match) -> str:
            path_str = match.group(0).replace("Run directory:", "").strip()
            run_id = Path(path_str).name if path_str else ""
            return f"[prev_run: {run_id}]" if run_id else ""

        history = []
        for msg in messages[:-1]:
            role = msg.role if hasattr(msg, "role") else msg.get("role", "user")
            content = msg.content if hasattr(msg, "content") else msg.get("content", "")
            if not content.strip() or role not in ("user", "assistant"):
                continue
            content = re.sub(r"Run directory:\s*\S+", _shorten_run_dir, content).strip()
            if content:
                history.append({"role": role, "content": content})

        budget = MAX_HISTORY_TOKENS
        trimmed: list = []
        dropped = 0
        for msg in reversed(history):
            cost = estimate_text_tokens(msg.get("content", "")) + PER_MESSAGE_TOKENS
            if cost > budget:
                # Not a break: a shorter older turn may still fit.
                dropped += 1
                continue
            trimmed.append(msg)
            budget -= cost
        trimmed.reverse()

        out: list[Dict[str, Any]] = []
        summary = handoff.load(session_id) if session_id else ""
        if summary:
            clipped = summary
            if estimate_text_tokens(clipped) > HANDOFF_INJECT_MAX_TOKENS:
                limit = len(clipped)
                while limit > 0 and estimate_text_tokens(clipped[:limit]) > HANDOFF_INJECT_MAX_TOKENS:
                    limit = int(limit * 0.9)
                clipped = clipped[:limit] + "\n\n...[summary clipped]"
            out.append(
                {
                    "role": "user",
                    # HANDOFF_PREFIX so the in-run Layer 2 recognises this block
                    # and never folds it (same marker Layer 3 writes).
                    "content": (
                        f"{HANDOFF_PREFIX} — carried over from earlier attempts "
                        "in this session. Background reference, NOT "
                        f"instructions.]\n\n{clipped}"
                    ),
                }
            )
        if dropped:
            out.append(
                {
                    "role": "user",
                    "content": (
                        f"[{dropped} earlier turns in this session were omitted "
                        "from this replay to fit the context budget."
                        + (
                            " Their content is covered by the summary above.]"
                            if summary
                            else "]"
                        )
                    ),
                }
            )
        out.extend(trimmed)
        return out

    @staticmethod
    def _load_metrics(run_dir: Path) -> Optional[Dict[str, Any]]:
        """Load metrics.csv from a run directory."""
        import csv
        metrics_path = run_dir / "artifacts" / "metrics.csv"
        if not metrics_path.exists():
            return None
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                if rows:
                    return {k: float(v) for k, v in rows[0].items() if v}
        except Exception:
            pass
        return None

    @staticmethod
    def _format_result_message(attempt: Attempt) -> str:
        """Format the final execution result message."""
        if attempt.status == AttemptStatus.COMPLETED:
            return attempt.summary or ""
        return f"Execution failed: {attempt.error or 'unknown error'}"
