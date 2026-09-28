"""ChatLLM: raw LLM message interface with function calling support.

ChatLLM is designed specifically for the AgentLoop ReAct cycle.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.providers.capabilities import ProviderCapabilities, get_provider_capabilities
from src.providers.llm import build_llm

# The only ``tool_choice`` value the engine sends. A forced text turn keeps
# the tool definitions in the payload and tells the model not to call any:
# the Anthropic Messages API rejects a request whose history contains
# tool_use / tool_result blocks but no ``tools`` (400), so dropping the
# definitions is not a way to force text there.
TOOL_CHOICE_NONE = "none"


def _content_text(content: Any) -> str:
    """Flatten message content to plain text.

    OpenAI-compat models return ``str``; the native Anthropic channel returns
    a list of typed blocks (thinking / text / tool_use). The engine's message
    history, trace, and answer paths all expect ``str`` — extract only the
    ``text`` blocks and drop thinking/tool_use (tool calls surface via
    ``.tool_calls``, thinking via ``reasoning_content``).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


def _content_thinking(content: Any) -> str:
    """Extract thinking text from native-Anthropic block-list content."""
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("thinking") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "thinking"
    )


# Native Anthropic stop_reason → OpenAI-style finish_reason the ReAct loop
# already understands (it compares against "tool_calls"/"stop"/"length").
_ANTHROPIC_STOP_REASON_MAP = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "refusal": "stop",
}


def _dedupe_finish_reason(raw: str) -> str:
    """Relays (OpenRouter) emit finish_reason per chunk; AIMessageChunk.__add__
    concatenates into 'stopstop', 'tool_callstool_calls', etc. Return the
    canonical suffix so ReAct equality checks survive.
    """
    return next(
        (m for m in ("tool_calls", "function_call", "content_filter", "length", "stop")
         if raw.endswith(m)),
        raw,
    )


@dataclass
class ToolCallRequest:
    """Tool call request returned by the LLM.

    Attributes:
        id: Tool call ID (used to match tool_result messages).
        name: Tool name.
        arguments: Tool argument dict.
        thought_signature: Gemini thinking-model signature to echo on the next turn.
    """

    id: str
    name: str
    arguments: Dict[str, Any]
    thought_signature: Optional[str] = None


@dataclass
class LLMResponse:
    """LLM response.

    Attributes:
        content: Text content (final answer or thinking text).
        tool_calls: List of tool call requests.
        reasoning_content: Optional thinking trace surfaced by reasoning models.
        finish_reason: Finish reason string.
        usage_metadata: Real token counts reported by the provider, when
            available. Mirrors LangChain's ``AIMessage.usage_metadata`` —
            ``{"input_tokens": int, "output_tokens": int, "total_tokens": int}``.
            ``None`` if the provider did not return usage information; callers
            should fall back to a heuristic in that case.
        interrupted: Why a stream stopped before the provider finished it:
            ``"cancelled"`` (``should_cancel``) or ``"deadline"`` (the call's
            wall-clock ``timeout`` ran out). ``None`` for a complete reply.
            An interrupted response is partial — its tool calls may carry
            half-written arguments and must not be executed.
    """

    content: Optional[str] = None
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    reasoning_content: Optional[str] = None
    finish_reason: str = "stop"
    usage_metadata: Optional[Dict[str, int]] = None
    interrupted: Optional[str] = None

    @property
    def has_tool_calls(self) -> bool:
        """Return True if the response contains tool calls."""
        return len(self.tool_calls) > 0


class ProviderStreamError(RuntimeError):
    """Raised when provider streaming fails before a complete response."""

    def __init__(self, *, provider: str, model: str, original: Exception) -> None:
        """Initialize a provider-contextual stream error.

        Args:
            provider: Effective provider name.
            model: Effective model name.
            original: Original exception from the stream path.
        """
        self.provider = provider
        self.model = model
        self.original = original
        self.status_code: Optional[int] = getattr(original, "status_code", None)
        safe_message = _redact_provider_error(str(original))
        super().__init__(
            f"provider_stream_error provider={provider} model={model}: "
            f"{type(original).__name__}: {safe_message}"
        )

    @property
    def retryable(self) -> bool:
        """Whether a single retry could plausibly succeed.

        Returns:
            False for deterministic client errors (4xx other than 408/429),
            True for everything else — timeouts, rate limits, 5xx, and
            transport errors that carry no HTTP status.
        """
        if self.status_code is None:
            return True
        if self.status_code in (408, 429):
            return True
        return not 400 <= self.status_code < 500


def _effective_provider() -> str:
    """Return the configured provider name (``LANGCHAIN_PROVIDER``, default openai)."""
    return os.getenv("LANGCHAIN_PROVIDER", "openai").strip().lower() or "openai"


def _redact_provider_error(message: str) -> str:
    """Redact configured secret/proxy values from provider errors."""
    redacted = message
    sensitive_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "PROXY")
    for key, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if any(marker in key.upper() for marker in sensitive_markers):
            redacted = redacted.replace(value, "[redacted]")
    return redacted


# Floor for a tightened per-request SDK timeout, so a nearly spent budget
# still allows the connection to be opened.
_MIN_REQUEST_TIMEOUT_S = 5.0


def _default_request_timeout_s() -> float:
    """The SDK request timeout every client is built with (``TIMEOUT_SECONDS``)."""
    try:
        return float(os.getenv("TIMEOUT_SECONDS", "120"))
    except ValueError:
        return 120.0


def _with_request_timeout(runnable: Any, seconds: Optional[float]) -> Any:
    """Tighten the SDK request timeout for one call; never loosen it.

    The value reaches the SDK's ``create(timeout=…)`` through LangChain's
    bound kwargs (both ``ChatAnthropic`` and ``ChatOpenAI`` merge call kwargs
    into the request payload). A ``RunnableConfig`` ``timeout`` key would
    not: LangChain files unknown config keys under ``configurable``, where no
    chat model reads them. For a stream this is the read timeout between
    bytes, not a total — the caller's per-chunk deadline check covers the
    total. Runnables without ``bind`` (the Codex adapter, test doubles) are
    returned unchanged.
    """
    if not seconds or seconds >= _default_request_timeout_s():
        return runnable
    bind = getattr(runnable, "bind", None)
    if not callable(bind):
        return runnable
    return bind(timeout=max(_MIN_REQUEST_TIMEOUT_S, float(seconds)))


class ChatLLM:
    """LLM chat client with function calling support.

    Uses build_llm() to obtain a ChatOpenAI instance and bind_tools() to attach tool definitions.

    Attributes:
        model_name: Model name.
    """

    def __init__(self, model_name: Optional[str] = None) -> None:
        """Initialize ChatLLM.

        Args:
            model_name: Model name; defaults to the environment variable value.
        """
        self.model_name = model_name
        self._llm = build_llm(model_name=model_name)
        self._provider = _effective_provider()
        self._caps: ProviderCapabilities = get_provider_capabilities(
            self._provider, model_name or os.getenv("LANGCHAIN_MODEL_NAME", "")
        )

    @property
    def provider(self) -> str:
        """Effective provider name (``LANGCHAIN_PROVIDER``, default openai)."""
        cached = getattr(self, "_provider", None)
        if cached is None:
            cached = self._provider = _effective_provider()
        return cached

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Capability record of the effective provider/model."""
        cached = getattr(self, "_caps", None)
        if cached is None:
            cached = self._caps = get_provider_capabilities(
                self.provider, self.model_name or os.getenv("LANGCHAIN_MODEL_NAME", "")
            )
        return cached

    @property
    def sends_reasoning_content(self) -> bool:
        """Whether assistant ``reasoning_content`` is sent back upstream.

        Only providers that require the field on multi-turn continuations
        (moonshot/kimi) count it toward the context estimate; every other
        channel drops it at request serialization, so counting it would
        inflate the estimate by the whole thinking transcript.
        """
        return self.capabilities.send_reasoning_content

    @property
    def supports_tool_choice_none(self) -> bool:
        """Whether a forced text turn can keep the tools and send tool_choice none."""
        return self.capabilities.tool_choice_none

    def _bind(self, tools: Optional[List[Dict[str, Any]]], tool_choice: Optional[str]) -> Any:
        """Return the model bound to ``tools`` honouring ``tool_choice``.

        Args:
            tools: Tool definitions, or None/empty for a bare call.
            tool_choice: ``None`` (model decides) or :data:`TOOL_CHOICE_NONE`.

        Returns:
            A runnable ready for ``invoke`` / ``stream``.
        """
        if not tools:
            return self._llm
        if tool_choice is None:
            return self._llm.bind_tools(tools)
        if tool_choice != TOOL_CHOICE_NONE:
            raise ValueError(f"unsupported tool_choice {tool_choice!r}")
        if not self.capabilities.tool_choice_none:
            # Provider fallback: omit the tools list for this turn.
            return self._llm
        choice: Any = {"type": TOOL_CHOICE_NONE} if self.provider == "anthropic" else TOOL_CHOICE_NONE
        return self._llm.bind_tools(tools, tool_choice=choice)

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        timeout: Optional[int] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        """Call the LLM synchronously.

        Args:
            messages: Message list (OpenAI format).
            tools: Tool definition list (OpenAI function calling format).
            timeout: Optional per-call timeout in seconds; tightens the SDK
                request timeout for this call (see ``_with_request_timeout``).
            tool_choice: ``None`` or :data:`TOOL_CHOICE_NONE` (see ``_bind``).

        Returns:
            LLMResponse.
        """
        llm = _with_request_timeout(self._bind(tools, tool_choice), timeout)
        ai_message = llm.invoke(messages)
        return self._parse_response(ai_message)

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_text_chunk: Optional[Any] = None,
        on_reasoning_chunk: Optional[Any] = None,
        timeout: Optional[float] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        """Stream the LLM and optionally forward text deltas (e.g. thinking).

        Iterates AIMessageChunk; text deltas invoke ``on_text_chunk`` and
        reasoning-only deltas invoke ``on_reasoning_chunk``. Aggregates chunks
        into one response. Stream failures are explicit provider errors.

        Args:
            messages: Messages in OpenAI format.
            tools: Tool definitions for function calling.
            on_text_chunk: Optional callback ``(delta: str) -> None``.
            on_reasoning_chunk: Optional callback ``(delta: str) -> None``.
            timeout: Optional wall-clock budget for the whole call, in
                seconds. Checked per chunk; once spent the stream is closed
                and the partial reply returned with ``interrupted="deadline"``
                (a transport error raised after that instant — the SDK's own
                read timeout, which this also tightens — ends the same way
                instead of raising). Silent stretches without any chunk are
                bounded by the SDK read timeout only.
            should_cancel: Optional predicate polled per chunk; when it returns
                True the stream stops early and the partial response is
                returned with ``interrupted="cancelled"``. Lets a caller abort
                a live stream promptly (cooperative cancel).
            tool_choice: ``None`` (model decides) or :data:`TOOL_CHOICE_NONE`
                for a forced text turn — tools stay in the payload, the model
                is told not to call any (providers without ``none`` support
                get the tools omitted instead, see capabilities).

        Returns:
            Parsed ``LLMResponse``.
        """
        deadline = time.monotonic() + timeout if timeout else None
        try:
            llm = _with_request_timeout(self._bind(tools, tool_choice), timeout)
        except Exception as exc:
            raise self._stream_error(exc) from exc
        return self._consume_stream(
            llm,
            messages,
            deadline=deadline,
            should_cancel=should_cancel,
            on_text_chunk=on_text_chunk,
            on_reasoning_chunk=on_reasoning_chunk,
        )

    def summarize(
        self,
        messages: List[Dict[str, Any]],
        *,
        timeout: Optional[float] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> LLMResponse:
        """Streamed, cancellable, retry-free call for context summaries.

        The Layer 3 compaction entry point. Streaming keeps bytes flowing
        through middleboxes during a long generation (the reason the native
        channel exists) and makes the call interruptible per chunk; the
        client behind it is built with ``max_retries=0`` because a failed
        summary is already a handled degradation — SDK retries would only
        multiply the time it can take.

        Args:
            messages: Messages in OpenAI format (no tools are bound).
            timeout: Wall-clock budget for the call, as in :meth:`stream_chat`.
            should_cancel: Cooperative cancel predicate, as in :meth:`stream_chat`.

        Returns:
            Parsed ``LLMResponse``; ``interrupted`` is set when cut short.
        """
        deadline = time.monotonic() + timeout if timeout else None
        llm = _with_request_timeout(self._summary_client(), timeout)
        return self._consume_stream(
            llm, messages, deadline=deadline, should_cancel=should_cancel
        )

    def _summary_client(self) -> Any:
        """The no-retry client behind :meth:`summarize` (built once, lazily)."""
        cached = getattr(self, "_summary_llm", None)
        if cached is not None:
            return cached
        try:
            cached = build_llm(model_name=self.model_name, max_retries=0)
        except Exception:  # noqa: BLE001 - fall back to the regular client
            cached = self._llm
        self._summary_llm = cached
        return cached

    def _stream_error(self, exc: Exception) -> ProviderStreamError:
        model = self.model_name or os.getenv("LANGCHAIN_MODEL_NAME", "").strip() or "(unset)"
        return ProviderStreamError(provider=self.provider, model=model, original=exc)

    def _consume_stream(
        self,
        llm: Any,
        messages: List[Dict[str, Any]],
        *,
        deadline: Optional[float],
        should_cancel: Optional[Callable[[], bool]],
        on_text_chunk: Optional[Any] = None,
        on_reasoning_chunk: Optional[Any] = None,
    ) -> LLMResponse:
        """Drain ``llm.stream`` into one response, honouring cancel and deadline."""
        accumulated = None
        interrupted: Optional[str] = None
        stream = None
        try:
            stream = llm.stream(messages)
            for chunk in stream:
                if should_cancel and should_cancel():
                    interrupted = "cancelled"
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    interrupted = "deadline"
                    break
                # Native Anthropic chunks carry block-list content; flatten to
                # text/thinking deltas so callers keep receiving plain strings.
                text_delta = _content_text(chunk.content) if chunk.content else ""
                if text_delta and on_text_chunk:
                    on_text_chunk(text_delta)
                reasoning = (
                    getattr(chunk, "additional_kwargs", {}).get("reasoning_content")
                    or _content_thinking(chunk.content)
                )
                if reasoning and not text_delta and on_reasoning_chunk:
                    on_reasoning_chunk(reasoning)
                accumulated = chunk if accumulated is None else accumulated + chunk
        except Exception as exc:
            if deadline is None or time.monotonic() < deadline:
                raise self._stream_error(exc) from exc
            # The call's budget is spent: a transport error now (typically
            # the tightened SDK read timeout) is the deadline, not a failure.
            interrupted = "deadline"
        finally:
            if interrupted is not None and stream is not None:
                # Close the HTTP stream now instead of at garbage collection,
                # so an abandoned generation stops being paid for.
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - best-effort close
                        pass
        if accumulated is None:
            return LLMResponse(content="", tool_calls=[], finish_reason="stop",
                               interrupted=interrupted)
        try:
            response = self._parse_response(accumulated)
        except Exception as exc:
            raise self._stream_error(exc) from exc
        response.interrupted = interrupted
        return response

    @staticmethod
    def _tool_call_thought_signature_maps(ai_message: Any) -> tuple[dict[str, str], dict[int, str]]:
        """Return Gemini thought signatures captured by ``ChatOpenAIWithReasoning``."""
        by_id: dict[str, str] = {}
        by_index: dict[int, str] = {}
        additional_kwargs = getattr(ai_message, "additional_kwargs", {})
        entries = additional_kwargs.get("tool_call_thought_signatures", [])

        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return by_id, by_index

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            signature = entry.get("thought_signature")
            if not signature:
                continue
            if entry.get("id"):
                by_id[str(entry["id"])] = signature
            index = entry.get("index")
            if isinstance(index, int):
                by_index[index] = signature
        return by_id, by_index

    @staticmethod
    def _parse_response(ai_message: Any) -> LLMResponse:
        """Convert a LangChain AIMessage (or AIMessageChunk) to ``LLMResponse``.

        Single source for reasoning: ``additional_kwargs["reasoning_content"]``,
        populated by ``ChatOpenAIWithReasoning`` on both stream and non-stream paths.

        ``usage_metadata`` is forwarded as-is from the underlying message so
        downstream cost / billing audit code (e.g. swarm worker token totals)
        can use real provider tokens instead of a character-count heuristic.
        For ``AIMessageChunk`` the metadata accumulates via the ``__add__``
        merge LangChain performs while the response is being streamed; the
        final aggregate carries the same shape as the non-stream path.
        """
        usage = getattr(ai_message, "usage_metadata", None)
        # Some providers / older LangChain versions surface a ``UsageMetadata``
        # TypedDict that doesn't json-serialise without a cast. Normalise to a
        # plain ``dict[str, int]`` so the value can be persisted alongside the
        # rest of the run state without surprises.
        if usage is not None and not isinstance(usage, dict):
            try:
                usage = dict(usage)
            except (TypeError, ValueError):
                usage = None
        thought_signatures_by_id, thought_signatures_by_index = (
            ChatLLM._tool_call_thought_signature_maps(ai_message)
        )
        # finish_reason: OpenAI channels set response_metadata["finish_reason"];
        # the native Anthropic channel sets "stop_reason" instead — map it to
        # the OpenAI vocabulary the ReAct loop compares against.
        meta = getattr(ai_message, "response_metadata", {}) or {}
        raw_finish = meta.get("finish_reason")
        if not raw_finish:
            stop_reason = meta.get("stop_reason")
            raw_finish = _ANTHROPIC_STOP_REASON_MAP.get(stop_reason, stop_reason or "stop")
        return LLMResponse(
            content=_content_text(ai_message.content),
            tool_calls=[
                ToolCallRequest(
                    id=tc["id"],
                    name=tc["name"],
                    arguments=tc["args"],
                    thought_signature=thought_signatures_by_id.get(str(tc["id"]))
                    or thought_signatures_by_index.get(index),
                )
                for index, tc in enumerate(ai_message.tool_calls)
            ],
            reasoning_content=(
                ai_message.additional_kwargs.get("reasoning_content")
                or (_content_thinking(ai_message.content) or None)
            ),
            finish_reason=_dedupe_finish_reason(raw_finish),
            usage_metadata=usage,
        )
