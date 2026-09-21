"""Provider capability definitions for OpenAI-compatible chat adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping, Optional


@dataclass(frozen=True)
class ProviderCapabilities:
    """Provider-specific payload and diagnostic behavior.

    Args:
        name: Canonical provider name.
        api_key_env: Provider-specific API key environment variable.
        base_url_env: Provider-specific base URL environment variable.
        capture_reasoning: Whether inbound reasoning fields should be preserved.
        send_reasoning_content: Whether outbound assistant history must include
            ``reasoning_content``.
        gemini_thought_signatures: Whether Gemini OpenAI-compatible tool-call
            thought signatures should be round-tripped.
        normalize_assistant_content: Whether assistant ``content=None`` should
            be normalized to ``""`` for strict providers.
        openrouter_reasoning_body: Whether ``extra_body.reasoning`` is a valid
            OpenRouter request option.
        tool_choice_none: Whether the endpoint accepts ``tool_choice="none"``
            (tools stay in the payload, the model is told not to call any).
            A forced text turn on a provider without it falls back to
            omitting the tools list instead — acceptable on OpenAI-compatible
            endpoints, which do not validate ``tool`` messages against the
            tools list the way the Anthropic Messages API does.
        default_headers: Provider-scoped headers passed to ChatOpenAI.
        native_adapter_package: Optional native adapter package to report.
    """

    name: str
    api_key_env: Optional[str]
    base_url_env: str
    capture_reasoning: bool = False
    send_reasoning_content: bool = False
    gemini_thought_signatures: bool = False
    normalize_assistant_content: bool = False
    openrouter_reasoning_body: bool = False
    tool_choice_none: bool = True
    default_headers: Mapping[str, str] = field(default_factory=dict)
    native_adapter_package: Optional[str] = None


# Distribution name from pyproject.toml [project].name.
_DISTRIBUTION_NAME = "vibe-trading-ai"


def _package_version() -> str:
    """Return the installed distribution version for User-Agent headers.

    Returns:
        Installed ``vibe-trading-ai`` version string, or ``"dev"`` when the
        package metadata is unavailable (e.g. an uninstalled source checkout).
    """
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "dev"


_KIMI_USER_AGENT = f"Vibe-Trading/{_package_version()}"


_MOONSHOT_CAPABILITIES = ProviderCapabilities(
    "moonshot",
    "MOONSHOT_API_KEY",
    "MOONSHOT_BASE_URL",
    capture_reasoning=True,
    send_reasoning_content=True,
    normalize_assistant_content=True,
    default_headers={"User-Agent": _KIMI_USER_AGENT},
)

# Zhipu's OpenAI-compatible endpoint documents ``tool_choice`` as "auto" only.
_ZHIPU_CAPABILITIES = ProviderCapabilities(
    "zhipu", "ZHIPU_API_KEY", "ZHIPU_BASE_URL", tool_choice_none=False
)

# Native Anthropic Messages channel (LANGCHAIN_PROVIDER=anthropic). Listed so
# the tool_choice / reasoning lookups are uniform; its env vars are read by
# ``llm._build_native_anthropic`` directly, never folded into OPENAI_*.
_ANTHROPIC_CAPABILITIES = ProviderCapabilities(
    "anthropic",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    native_adapter_package="langchain-anthropic",
)

_OPENAI_CODEX_CAPABILITIES = ProviderCapabilities("openai-codex", None, "OPENAI_CODEX_BASE_URL")


_PROVIDERS: dict[str, ProviderCapabilities] = {
    "openai": ProviderCapabilities("openai", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "anthropic": _ANTHROPIC_CAPABILITIES,
    "openrouter": ProviderCapabilities(
        "openrouter",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        capture_reasoning=True,
        openrouter_reasoning_body=True,
    ),
    "deepseek": ProviderCapabilities(
        "deepseek",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        capture_reasoning=True,
        native_adapter_package="langchain-deepseek",
    ),
    "gemini": ProviderCapabilities(
        "gemini",
        "GEMINI_API_KEY",
        "GEMINI_BASE_URL",
        gemini_thought_signatures=True,
    ),
    "groq": ProviderCapabilities("groq", "GROQ_API_KEY", "GROQ_BASE_URL"),
    "dashscope": ProviderCapabilities("dashscope", "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL"),
    "qwen": ProviderCapabilities("qwen", "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL"),
    "zhipu": _ZHIPU_CAPABILITIES,
    "glm": _ZHIPU_CAPABILITIES,
    "moonshot": _MOONSHOT_CAPABILITIES,
    "kimi": _MOONSHOT_CAPABILITIES,
    "minimax": ProviderCapabilities("minimax", "MINIMAX_API_KEY", "MINIMAX_BASE_URL"),
    "mimo": ProviderCapabilities("mimo", "MIMO_API_KEY", "MIMO_BASE_URL"),
    "zai": ProviderCapabilities("zai", "ZAI_API_KEY", "ZAI_BASE_URL"),
    "ollama": ProviderCapabilities("ollama", None, "OLLAMA_BASE_URL"),
    "openai-codex": _OPENAI_CODEX_CAPABILITIES,
    "openai_codex": _OPENAI_CODEX_CAPABILITIES,
}


def _infer_from_model(model: str) -> str | None:
    lowered = model.strip().lower()
    if not lowered:
        return None
    if lowered.startswith("gemini"):
        return "gemini"
    if lowered.startswith("deepseek"):
        return "deepseek"
    if lowered.startswith("glm"):
        return "zhipu"
    if "kimi" in lowered or "moonshot" in lowered:
        return "moonshot"
    return None


def get_provider_capabilities(
    provider: str | None = None,
    model: str | None = None,
) -> ProviderCapabilities:
    """Return the capability record for a provider/model pair.

    Args:
        provider: Configured provider name.
        model: Configured model name, used for direct test/adapter inference.

    Returns:
        Provider capability definition. Unknown providers fall back to OpenAI.
    """
    normalized = (provider or "").strip().lower().replace("_", "-")
    if normalized == "openai-codex":
        return _PROVIDERS["openai-codex"]
    if normalized and normalized != "openai":
        return _PROVIDERS.get(normalized, _PROVIDERS["openai"])
    inferred = _infer_from_model(model or "")
    if inferred:
        return _PROVIDERS[inferred]
    return _PROVIDERS.get(normalized, _PROVIDERS["openai"])


def provider_env_names(provider: str | None, model: str | None = None) -> tuple[str | None, str]:
    """Return the API-key and base-URL env names for a provider/model pair."""
    caps = get_provider_capabilities(provider, model)
    return caps.api_key_env, caps.base_url_env
