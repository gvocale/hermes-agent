"""Gateway runtime-metadata footer, off by default to keep replies
minimal. Config: ``display.runtime_footer: {enabled: bool, fields: [model, context_pct, cwd]}``
(order shown; drop any to hide), per-platform override ``display.platforms.<p>.runtime_footer``,
toggled by ``/footer on|off``. Fields: ``model`` (vendor prefix dropped), ``context_pct`` (last-call
occupancy), ``reasoning``, ``latency``, rolling ``tps``, session ``cache_hit`` and
``total_tokens`` (all opt-in), and ``cwd`` (home-relative). ``gateway/run.py`` appends the footer to the
final response only (never to tool-progress or streaming partials); when streaming already
delivered the text, it goes out as a trailing message via ``send_trailing_footer()``."""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "

_SLACK_MODEL_EMOJI_MARKERS = (
    (":openai:", ("openai", "chatgpt", "codex", "gpt-")),
    (":grok:", ("grok", "xai")),
    (":claude:", ("claude", "anthropic", "fable")),
)


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    return model.rsplit("/", 1)[-1] if model else ""


def slack_model_emoji(model: Optional[str], provider: Optional[str] = None) -> str:
    """Return the custom Slack emoji for a resolved model identity, if known."""
    for identity in (model, provider):
        value = str(identity or "").lower()
        for emoji, markers in _SLACK_MODEL_EMOJI_MARKERS:
            if any(marker in value for marker in markers):
                return emoji
    return ""


def _env_cwd() -> str:
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def resolve_footer_config(user_config: dict[str, Any] | None, platform_key: str | None = None) -> dict[str, Any]:
    """Resolve effective footer config: defaults (enabled=False) <
    ``display.runtime_footer`` < ``display.platforms.<platform_key>.runtime_footer``."""
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}
    plat_cfg = (cfg.get("platforms") or {}).get(platform_key) if platform_key else None
    sections = [cfg.get("runtime_footer"), plat_cfg.get("runtime_footer") if isinstance(plat_cfg, dict) else None]
    for section in sections:
        if not isinstance(section, dict):
            continue
        if "enabled" in section:
            resolved["enabled"] = bool(section.get("enabled"))
        if isinstance(section.get("fields"), list) and section["fields"]:
            resolved["fields"] = [str(f) for f in section["fields"]]
    return resolved


def _format_latency(seconds: float) -> str:
    """Humanize a turn duration: ``<1s``, ``22s``, ``1m05s``."""
    if seconds < 1:
        return "<1s"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, sec = divmod(total, 60)
    return f"{m}m{sec:02d}s"


def _compact_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    for divisor, suffix in ((1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")):
        if value >= divisor:
            return f"{value / divisor:.1f}{suffix}"
    return str(value)


def format_runtime_footer(*, model: Optional[str], context_tokens: int,
                          context_length: Optional[int], cwd: Optional[str] = None,
                          turn_seconds: Optional[float] = None, reasoning: Optional[str] = None,
                          api_latency_history: Iterable[float] = (),
                          api_output_history: Iterable[int] = (),
                          session_prompt_tokens: int = 0, session_cache_read_tokens: int = 0,
                          session_total_tokens: int = 0,
                          fields: Iterable[str] = _DEFAULT_FIELDS,
                          model_prefix: str = "") -> str:
    """Render the footer line, or "" if no fields have data. Fields whose data is missing (and
    unknown field names) are skipped silently — a partial footer beats ``?%`` or empty slots."""
    def context_pct() -> str:
        if context_length and context_length > 0 and context_tokens >= 0:
            return f"{max(0, min(100, round((context_tokens / context_length) * 100)))}%"
        return ""

    latencies = list(api_latency_history)
    outputs = list(api_output_history)
    sample_count = min(len(latencies), len(outputs))
    total_api_seconds = sum(latencies[-sample_count:]) if sample_count else 0
    tps = sum(outputs[-sample_count:]) / total_api_seconds if total_api_seconds > 0 else None
    cache_hit = (session_cache_read_tokens / session_prompt_tokens * 100
                 if session_prompt_tokens > 0 and session_cache_read_tokens > 0 else None)

    renderers = {
        "model": lambda: f"{model_prefix} {_model_short(model)}" if model_prefix and model else _model_short(model),
        "reasoning": lambda: reasoning or "",
        "context_pct": context_pct,
        # Skipped when the caller did not measure (None) or the value is negative.
        "latency": lambda: _format_latency(turn_seconds) if turn_seconds is not None and turn_seconds >= 0 else "",
        "tps": lambda: f"{tps:.0f} t/s" if tps is not None and 0 < tps < 1e6 else "",
        "cache_hit": lambda: f"{max(0, min(100, round(cache_hit)))}% hit" if cache_hit is not None else "",
        "total_tokens": lambda: f"Σ{_compact_count(session_total_tokens)}" if session_total_tokens > 0 else "",
        "cwd": lambda: _home_relative_cwd(cwd or _env_cwd()),
    }
    return _SEP.join(v for field in fields if (render := renderers.get(field)) and (v := render()))


def build_footer_line(*, user_config: dict[str, Any] | None, platform_key: str | None,
                      model: Optional[str], context_tokens: int, context_length: Optional[int],
                      cwd: Optional[str] = None, turn_seconds: Optional[float] = None,
                      reasoning: Optional[str] = None,
                      api_latency_history: Iterable[float] = (),
                      api_output_history: Iterable[int] = (),
                      session_prompt_tokens: int = 0, session_cache_read_tokens: int = 0,
                      session_total_tokens: int = 0) -> str:
    """Entry point for gateway/run.py: footer text, or "" when disabled / no data. Callers append it
    to the final response themselves, preserving a single blank line of separation.
    ``turn_seconds`` is the caller-measured (``time.monotonic()``) run duration; ``None`` skips the
    ``latency`` field."""
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    model_prefix = slack_model_emoji(model) if platform_key == "slack" else ""
    return format_runtime_footer(model=model, context_tokens=context_tokens,
                                 context_length=context_length, cwd=cwd, turn_seconds=turn_seconds,
                                 reasoning=reasoning, api_latency_history=api_latency_history,
                                 api_output_history=api_output_history,
                                 session_prompt_tokens=session_prompt_tokens,
                                 session_cache_read_tokens=session_cache_read_tokens,
                                 session_total_tokens=session_total_tokens,
                                 fields=cfg.get("fields") or _DEFAULT_FIELDS,
                                 model_prefix=model_prefix)
