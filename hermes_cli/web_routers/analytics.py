"""Raw-YAML config and token/cost analytics dashboard routes.

Extracted from ``hermes_cli.web_server``; app state and helpers are late-bound through
:mod:`hermes_cli.web_deps` (cycle-safe, monkeypatch-friendly).
"""

import asyncio
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException, Query

from hermes_cli.config import get_config_path, read_raw_config
from hermes_cli.web_deps import late
from hermes_cli.web_server_profiles import (
    _approval_mode_of, _aux_task_summary, _aux_usage_rows, _broadcast_gateway_session_info, _is_other_profile,
)
from hermes_cli.web_models import RawConfigUpdate

router = APIRouter()

# Late-bound so a test's monkeypatch on the owning module wins at call time.
_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")
_profile_scope = late("_profile_scope", "hermes_cli.web_server_profiles")
_config_profile_scope = late("_config_profile_scope", "hermes_cli.web_server_profiles")
save_config = late("save_config", "hermes_cli.config")

# ── Raw YAML config ──────────────────────────────────────────────────────────


@router.get("/api/config/raw")
async def get_config_raw(profile: Optional[str] = None):
    """Raw config.yaml text plus its resolved path.

    ``path`` is resolved inside ``_profile_scope`` so the Config page header
    shows the file the switched profile actually reads/writes — /api/status's
    ``config_path`` is machine-global and always reports the dashboard
    process's own profile, which is wrong under the global profile switcher.
    """
    def _run():
        with _profile_scope(profile):
            path = get_config_path()
        if not path.exists():
            return {"yaml": "", "path": str(path)}
        return {"yaml": path.read_text(encoding="utf-8"), "path": str(path)}

    return await asyncio.to_thread(_run)


@router.put("/api/config/raw")
async def update_config_raw(body: RawConfigUpdate, profile: Optional[str] = None):
    def _run():
        parsed = yaml.safe_load(body.yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        with _profile_scope(body.profile or profile):
            # Full-document replacement: the editor owns the whole file; never
            # merge omitted sections back from disk.
            # See #62723.
            approvals_mode_changed = _approval_mode_of(parsed) != _approval_mode_of(read_raw_config())
            save_config(parsed, merge_existing=False)
        # Same indicator refresh as the schema-driven save.
        if approvals_mode_changed and not _is_other_profile(body.profile or profile):
            _broadcast_gateway_session_info()
        return {"ok": True}

    try:
        return await asyncio.to_thread(_run)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")


def _rows(db, sql: str, *params: float) -> List[Dict[str, Any]]:
    return [dict(r) for r in db._conn.execute(sql, params).fetchall()]


def _get_usage_analytics(days: int = 30, profile: Optional[str] = None):
    from agent.insights import InsightsEngine

    db = _open_session_db_for_profile(profile, read_only=True)
    try:
        cutoff = time.time() - (days * 86400)
        daily = _rows(db, """
            SELECT date(started_at, 'unixepoch') as day,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ?
            GROUP BY day ORDER BY day
        """, cutoff)

        by_model = _rows(db, """
            SELECT u.model,
                   u.billing_provider as provider,
                   SUM(u.input_tokens) as input_tokens,
                   SUM(u.output_tokens) as output_tokens,
                   COALESCE(SUM(u.estimated_cost_usd), 0) as estimated_cost,
                   COUNT(DISTINCT u.session_id) as sessions,
                   SUM(COALESCE(u.api_call_count, 0)) as api_calls
            FROM session_model_usage u
            JOIN sessions s ON s.id = u.session_id
            WHERE s.started_at > ? AND u.model IS NOT NULL AND u.model != ''
            GROUP BY u.model, u.billing_provider
            ORDER BY SUM(u.input_tokens) + SUM(u.output_tokens) DESC
        """, cutoff)

        # Gateway sessions report cumulative totals (absolute=True), so they do not
        # create session_model_usage rows. Add only the portion of each session that
        # has not already been attributed to a per-call model/provider row.
        residual = _rows(db, """
            SELECT s.model,
                   s.billing_provider as provider,
                   SUM(CASE WHEN COALESCE(s.input_tokens, 0) > COALESCE(m.input_tokens, 0)
                            THEN COALESCE(s.input_tokens, 0) - COALESCE(m.input_tokens, 0)
                            ELSE 0 END) as input_tokens,
                   SUM(CASE WHEN COALESCE(s.output_tokens, 0) > COALESCE(m.output_tokens, 0)
                            THEN COALESCE(s.output_tokens, 0) - COALESCE(m.output_tokens, 0)
                            ELSE 0 END) as output_tokens,
                   SUM(CASE WHEN COALESCE(s.estimated_cost_usd, 0) > COALESCE(m.estimated_cost, 0)
                            THEN COALESCE(s.estimated_cost_usd, 0) - COALESCE(m.estimated_cost, 0)
                            ELSE 0 END) as estimated_cost,
                   SUM(CASE WHEN m.session_id IS NULL THEN 1 ELSE 0 END) as sessions,
                   SUM(CASE WHEN COALESCE(s.api_call_count, 0) > COALESCE(m.api_calls, 0)
                            THEN COALESCE(s.api_call_count, 0) - COALESCE(m.api_calls, 0)
                            ELSE 0 END) as api_calls
            FROM sessions s
            LEFT JOIN (
                SELECT session_id,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                       SUM(COALESCE(api_call_count, 0)) as api_calls
                FROM session_model_usage
                WHERE task = ''
                GROUP BY session_id
            ) m ON m.session_id = s.id
            WHERE s.started_at > ? AND s.model IS NOT NULL AND s.model != ''
            GROUP BY s.model, s.billing_provider
        """, cutoff)
        merged = {
            (row.get("model") or "", row.get("provider") or ""): dict(row)
            for row in by_model
        }
        for row in residual:
            if not any((row.get(key) or 0) for key in (
                "input_tokens", "output_tokens", "estimated_cost", "sessions", "api_calls",
            )):
                continue
            key = (row.get("model") or "", row.get("provider") or "")
            target = merged.setdefault(key, {
                "model": row.get("model") or "unknown",
                "provider": row.get("provider") or "",
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost": 0,
                "sessions": 0,
                "api_calls": 0,
            })
            for field in ("input_tokens", "output_tokens", "estimated_cost", "sessions", "api_calls"):
                target[field] = (target.get(field) or 0) + (row.get(field) or 0)
        by_model = sorted(
            merged.values(),
            key=lambda row: (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0),
            reverse=True,
        )

        # Aux task summary only (tokens already in by_model via session_model_usage).
        aux_rows = _aux_usage_rows(db, cutoff)

        totals = _rows(db, """
            SELECT SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ?
        """, cutoff)[0]
        usage = InsightsEngine(db).get_usage_breakdown(days=days)

        return {
            "daily": daily,
            "by_model": by_model,
            "by_task": _aux_task_summary(aux_rows),  # "what is compression costing me"
            "totals": totals,
            "period_days": days,
            "skills": usage["skills"],
            "tools": usage["tools"],  # per-tool-name counts; desktop aggregates per toolset
        }
    finally:
        db.close()


@router.get("/api/analytics/usage")
async def get_usage_analytics(
    days: int = Query(30, ge=1, le=365),
    profile: Optional[str] = None,
):
    """``days`` is clamped to 1-365 (idea from #74778): huge or non-positive
    values would force expensive full-history SQL and InsightsEngine work, or
    produce empty/inverted time windows. The UI only offers 7/30/90-day
    presets."""
    return await asyncio.to_thread(_get_usage_analytics, days, profile)


_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens",
    "estimated_cost", "actual_cost", "api_calls", "tool_calls",
)


def _has_usage(row: Dict[str, Any]) -> bool:
    return any((row.get(key) or 0) != 0 for key in _USAGE_KEYS)


def _fold_session_only_rows(raw_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold model rows that carry no billing_provider and no usage into the single
    accounted provider row for that model.

    Session rows can be created before the first billable call finishes; if that early row
    records only the model name while a later row has real accounting, the Models page used
    to show a duplicate "0 tokens / — API calls" card. Only folds when ownership is
    unambiguous (exactly one provider row).
    """
    rows_by_model: Dict[str, List[Dict[str, Any]]] = {}
    for row in raw_rows:
        rows_by_model.setdefault(row.get("model") or "", []).append(row)

    rows: List[Dict[str, Any]] = []
    for model_rows in rows_by_model.values():
        provider_rows = [r for r in model_rows if r.get("billing_provider")]
        if len(provider_rows) != 1:
            rows.extend(model_rows)
            continue
        target = provider_rows[0]
        for row in model_rows:
            if row is target or row.get("billing_provider") or _has_usage(row):
                continue
            target["sessions"] = (target.get("sessions") or 0) + (row.get("sessions") or 0)
            target["last_used_at"] = max(target.get("last_used_at") or 0, row.get("last_used_at") or 0)
            total_tokens = (target.get("input_tokens") or 0) + (target.get("output_tokens") or 0)
            sessions = target.get("sessions") or 0
            target["avg_tokens_per_session"] = total_tokens / sessions if sessions else 0
        rows.append(target)
        rows.extend(
            r for r in model_rows
            if r is not target and (r.get("billing_provider") or _has_usage(r))
        )
    return rows


def _model_capabilities(provider: str, model_name: str) -> dict:
    """models.dev capability metadata for the card; {} when unknown or lookup fails."""
    try:
        from agent.models_dev import get_model_capabilities
        mc = get_model_capabilities(provider=provider, model=model_name, allow_network=True)
    except Exception:
        return {}
    if mc is None:
        return {}
    return {
        "supports_tools": mc.supports_tools,
        "supports_vision": mc.supports_vision,
        "supports_reasoning": mc.supports_reasoning,
        "context_window": mc.context_window,
        "max_output_tokens": mc.max_output_tokens,
        "model_family": mc.model_family,
    }


_AUX_SUMMED_KEYS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens", "estimated_cost", "sessions", "api_calls",
)
_MODEL_CARD_KEYS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens",
    "estimated_cost", "actual_cost", "sessions", "api_calls", "tool_calls",
    "last_used_at", "avg_tokens_per_session",
)


def _merge_usage_rows(base: List[Dict[str, Any]], extra: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add extra (model, billing_provider) rows into base by summing numeric keys."""
    merged: Dict[tuple, Dict[str, Any]] = {}
    for row in base + extra:
        key = (row.get("model") or "", row.get("billing_provider") or "")
        target = merged.get(key)
        if target is None:
            merged[key] = dict(row)
            continue
        for field in _MODEL_CARD_KEYS:
            if field == "last_used_at":
                target[field] = max(target.get(field) or 0, row.get(field) or 0)
            elif field == "avg_tokens_per_session":
                continue
            else:
                target[field] = (target.get(field) or 0) + (row.get(field) or 0)
        total_tokens = (target.get("input_tokens") or 0) + (target.get("output_tokens") or 0)
        sessions = target.get("sessions") or 0
        target["avg_tokens_per_session"] = total_tokens / sessions if sessions else 0
    return list(merged.values())


def _get_models_analytics(days: int = 30, profile: Optional[str] = None):
    """Per-model token/cost/session breakdown plus models.dev capability metadata."""
    db = _open_session_db_for_profile(profile, read_only=True)
    try:
        cutoff = time.time() - (days * 86400)

        # Per-call attribution lives in session_model_usage. The sessions row keeps
        # only the last/main (model, billing_provider), so grouping sessions dumps
        # mixed-provider usage onto one card.
        try:
            raw_rows = _rows(db, """
                SELECT u.model,
                       u.billing_provider,
                       SUM(u.input_tokens) as input_tokens,
                       SUM(u.output_tokens) as output_tokens,
                       SUM(u.cache_read_tokens) as cache_read_tokens,
                       SUM(u.reasoning_tokens) as reasoning_tokens,
                       COALESCE(SUM(u.estimated_cost_usd), 0) as estimated_cost,
                       COALESCE(SUM(u.actual_cost_usd), 0) as actual_cost,
                       COUNT(DISTINCT u.session_id) as sessions,
                       SUM(COALESCE(u.api_call_count, 0)) as api_calls,
                       0 as tool_calls,
                       MAX(u.last_seen) as last_used_at,
                       AVG(u.input_tokens + u.output_tokens) as avg_tokens_per_session
                FROM session_model_usage u
                JOIN sessions s ON s.id = u.session_id
                WHERE s.started_at > ? AND u.task = ''
                  AND u.model IS NOT NULL AND u.model != ''
                GROUP BY u.model, u.billing_provider
                ORDER BY SUM(u.input_tokens) + SUM(u.output_tokens) DESC
            """, cutoff)
        except Exception:
            raw_rows = []

        # Legacy / absolute-only sessions: tokens on the sessions row not yet
        # attributed in session_model_usage (task='') belong to the session route.
        try:
            residual = _rows(db, """
                SELECT s.model,
                       s.billing_provider,
                       SUM(CASE WHEN COALESCE(s.input_tokens, 0) > COALESCE(m.input_tokens, 0)
                                THEN COALESCE(s.input_tokens, 0) - COALESCE(m.input_tokens, 0)
                                ELSE 0 END) as input_tokens,
                       SUM(CASE WHEN COALESCE(s.output_tokens, 0) > COALESCE(m.output_tokens, 0)
                                THEN COALESCE(s.output_tokens, 0) - COALESCE(m.output_tokens, 0)
                                ELSE 0 END) as output_tokens,
                       SUM(CASE WHEN COALESCE(s.cache_read_tokens, 0) > COALESCE(m.cache_read_tokens, 0)
                                THEN COALESCE(s.cache_read_tokens, 0) - COALESCE(m.cache_read_tokens, 0)
                                ELSE 0 END) as cache_read_tokens,
                       SUM(CASE WHEN COALESCE(s.reasoning_tokens, 0) > COALESCE(m.reasoning_tokens, 0)
                                THEN COALESCE(s.reasoning_tokens, 0) - COALESCE(m.reasoning_tokens, 0)
                                ELSE 0 END) as reasoning_tokens,
                       SUM(CASE WHEN COALESCE(s.estimated_cost_usd, 0) > COALESCE(m.estimated_cost, 0)
                                THEN COALESCE(s.estimated_cost_usd, 0) - COALESCE(m.estimated_cost, 0)
                                ELSE 0 END) as estimated_cost,
                       SUM(CASE WHEN COALESCE(s.actual_cost_usd, 0) > COALESCE(m.actual_cost, 0)
                                THEN COALESCE(s.actual_cost_usd, 0) - COALESCE(m.actual_cost, 0)
                                ELSE 0 END) as actual_cost,
                       SUM(CASE WHEN m.session_id IS NULL THEN 1 ELSE 0 END) as sessions,
                       SUM(CASE WHEN COALESCE(s.api_call_count, 0) > COALESCE(m.api_calls, 0)
                                THEN COALESCE(s.api_call_count, 0) - COALESCE(m.api_calls, 0)
                                ELSE 0 END) as api_calls,
                       SUM(COALESCE(s.tool_call_count, 0)) as tool_calls,
                       MAX(s.started_at) as last_used_at,
                       0 as avg_tokens_per_session
                FROM sessions s
                LEFT JOIN (
                    SELECT session_id,
                           SUM(input_tokens) as input_tokens,
                           SUM(output_tokens) as output_tokens,
                           SUM(cache_read_tokens) as cache_read_tokens,
                           SUM(reasoning_tokens) as reasoning_tokens,
                           COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                           COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                           SUM(COALESCE(api_call_count, 0)) as api_calls
                    FROM session_model_usage
                    WHERE task = ''
                    GROUP BY session_id
                ) m ON m.session_id = s.id
                WHERE s.started_at > ? AND s.model IS NOT NULL AND s.model != ''
                GROUP BY s.model, s.billing_provider
            """, cutoff)
        except Exception:
            residual = _rows(db, """
                SELECT model,
                       billing_provider,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cache_read_tokens) as cache_read_tokens,
                       SUM(reasoning_tokens) as reasoning_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                       COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                       COUNT(*) as sessions,
                       SUM(COALESCE(api_call_count, 0)) as api_calls,
                       SUM(tool_call_count) as tool_calls,
                       MAX(started_at) as last_used_at,
                       AVG(input_tokens + output_tokens) as avg_tokens_per_session
                FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
                GROUP BY model, billing_provider
            """, cutoff)
        residual = [
            row for row in residual
            if any((row.get(k) or 0) != 0 for k in _USAGE_KEYS) or (row.get("sessions") or 0)
        ]
        raw_rows = _merge_usage_rows(raw_rows, residual)

        # Aux-only models (dedicated vision/compression) as (model, provider) rows,
        # keyed like the GROUP BY above, so they appear on the Models page.
        # See #23270.
        for aux in _aux_usage_rows(db, cutoff):
            raw_rows.append({
                "model": aux.get("model") or "unknown",
                "billing_provider": aux.get("billing_provider") or "",
                **{key: aux.get(key) or 0 for key in _AUX_SUMMED_KEYS},
                "actual_cost": 0,
                "tool_calls": 0,
                "last_used_at": aux.get("last_used_at"),
                "avg_tokens_per_session": 0,
                "aux_task": aux.get("task") or "",
            })

        # Aux usage may share the main model/provider. Merge once more after
        # appending it so one billing identity always produces one card.
        rows = _fold_session_only_rows(_merge_usage_rows([], raw_rows))

        # Aggregated main/aux rows each carry their own DISTINCT session count.
        # Recompute across their union so one physical session is counted once
        # per model/provider card, while retaining absolute-only session routes.
        session_count_rows = _rows(db, """
            WITH main_usage AS (
                SELECT session_id,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cache_read_tokens) as cache_read_tokens,
                       SUM(reasoning_tokens) as reasoning_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                       COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                       SUM(COALESCE(api_call_count, 0)) as api_calls
                FROM session_model_usage
                WHERE task = ''
                GROUP BY session_id
            ), card_sessions AS (
                SELECT u.model, u.billing_provider, u.session_id
                FROM session_model_usage u
                JOIN sessions s ON s.id = u.session_id
                WHERE s.started_at > ? AND u.model IS NOT NULL AND u.model != ''
                UNION
                SELECT s.model, s.billing_provider, s.id
                FROM sessions s
                LEFT JOIN main_usage m ON m.session_id = s.id
                WHERE s.started_at > ? AND s.model IS NOT NULL AND s.model != ''
                  AND (
                    COALESCE(s.input_tokens, 0) > COALESCE(m.input_tokens, 0)
                    OR COALESCE(s.output_tokens, 0) > COALESCE(m.output_tokens, 0)
                    OR COALESCE(s.cache_read_tokens, 0) > COALESCE(m.cache_read_tokens, 0)
                    OR COALESCE(s.reasoning_tokens, 0) > COALESCE(m.reasoning_tokens, 0)
                    OR COALESCE(s.estimated_cost_usd, 0) > COALESCE(m.estimated_cost, 0)
                    OR COALESCE(s.actual_cost_usd, 0) > COALESCE(m.actual_cost, 0)
                    OR COALESCE(s.api_call_count, 0) > COALESCE(m.api_calls, 0)
                    OR COALESCE(s.tool_call_count, 0) > 0
                  )
            )
            SELECT model, billing_provider, COUNT(DISTINCT session_id) as sessions
            FROM card_sessions
            GROUP BY model, billing_provider
        """, cutoff, cutoff)
        session_counts = {
            (row.get("model") or "", row.get("billing_provider") or ""): row.get("sessions") or 0
            for row in session_count_rows
        }
        for row in rows:
            key = (row.get("model") or "", row.get("billing_provider") or "")
            if key in session_counts:
                row["sessions"] = session_counts[key]
                total_tokens = (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0)
                row["avg_tokens_per_session"] = total_tokens / row["sessions"] if row["sessions"] else 0
        rows.sort(
            key=lambda r: (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0),
            reverse=True,
        )

        models = [
            {
                "model": row["model"],
                "provider": row.get("billing_provider") or "",
                **{key: row[key] for key in _MODEL_CARD_KEYS},
                "capabilities": _model_capabilities(row.get("billing_provider") or "", row["model"]),
            }
            for row in rows
        ]

        totals = _rows(db, """
            SELECT COUNT(DISTINCT model) as distinct_models,
                   SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
        """, cutoff)[0]

        return {"models": models, "totals": totals, "period_days": days}
    finally:
        db.close()


@router.get("/api/analytics/models")
async def get_models_analytics(
    days: int = Query(30, ge=1, le=365),
    profile: Optional[str] = None,
):
    """Return model analytics without blocking the serving event loop."""
    return await asyncio.to_thread(_get_models_analytics, days, profile)


_CAPACITY_TTL_S = 30.0
_capacity_lock = threading.Lock()
_capacity_snapshots: Dict[str, tuple[float, Dict[str, Any]]] = {}
_capacity_inflight: Dict[str, Future] = {}


def reset_provider_capacity_cache() -> None:
    with _capacity_lock:
        _capacity_snapshots.clear()


def _list_capacity_providers(profile: Optional[str] = None) -> List[str]:
    """Configured + recently billed providers (deduped, no empty names)."""
    names: List[str] = []
    try:
        from hermes_cli.config import load_config
        from hermes_cli.models import normalize_provider

        with _config_profile_scope(profile):
            cfg = load_config() or {}
        model_cfg = cfg.get("model") if isinstance(cfg, dict) else None
        if isinstance(model_cfg, dict):
            raw = str(model_cfg.get("provider") or "").strip()
            if raw:
                names.append(normalize_provider(raw) or raw)
    except Exception:
        pass
    try:
        db = _open_session_db_for_profile(profile, read_only=True)
        try:
            cutoff = time.time() - (30 * 86400)
            rows = db._conn.execute(
                """
                SELECT billing_provider FROM (
                    SELECT u.billing_provider AS billing_provider
                    FROM session_model_usage u
                    JOIN sessions s ON s.id = u.session_id
                    WHERE s.started_at > ? AND COALESCE(u.billing_provider, '') != ''
                    UNION
                    SELECT s.billing_provider AS billing_provider
                    FROM sessions s
                    WHERE s.started_at > ? AND COALESCE(s.billing_provider, '') != ''
                )
                """,
                (cutoff, cutoff),
            ).fetchall()
            for row in rows:
                val = str(row[0] if not isinstance(row, dict) else row.get("billing_provider") or "").strip()
                if val:
                    names.append(val)
        finally:
            db.close()
    except Exception:
        pass
    out: List[str] = []
    seen = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _capacity_window_dict(window: Any) -> Dict[str, Any]:
    reset = getattr(window, "reset_at", None)
    return {
        "label": getattr(window, "label", ""),
        "used_percent": getattr(window, "used_percent", None),
        "reset_at": reset.isoformat() if reset is not None else None,
        "detail": getattr(window, "detail", None),
    }


def _fetch_one_capacity(provider: str, profile: Optional[str] = None) -> Dict[str, Any]:
    from agent.account_usage import fetch_account_usage

    try:
        # Worker threads do not inherit the request's contextvars. Establish
        # the selected profile inside each worker before credentials/config load.
        with _config_profile_scope(profile):
            snap = fetch_account_usage(provider)
    except Exception as exc:
        return {
            "id": provider,
            "windows": [],
            "fetched_at": None,
            "unavailable_reason": str(exc),
            "plan": None,
            "details": [],
        }
    if snap is None:
        return {
            "id": provider,
            "windows": [],
            "fetched_at": None,
            "unavailable_reason": "no snapshot",
            "plan": None,
            "details": [],
        }
    fetched = getattr(snap, "fetched_at", None)
    return {
        "id": provider,
        "windows": [_capacity_window_dict(w) for w in (snap.windows or ())],
        "fetched_at": fetched.isoformat() if fetched is not None else None,
        "unavailable_reason": snap.unavailable_reason,
        "plan": snap.plan,
        "details": list(snap.details or ()),
        "title": snap.title,
    }


def _compute_provider_capacity(profile: Optional[str] = None) -> Dict[str, Any]:
    providers = _list_capacity_providers(profile)
    rows: List[Dict[str, Any]] = []
    if providers:
        workers = min(8, len(providers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_fetch_one_capacity, p, profile) for p in providers]
            for fut in as_completed(futs):
                rows.append(fut.result())
    rows.sort(key=lambda r: r.get("id") or "")
    return {"providers": rows, "fetched_at": time.time(), "cached": False}


def _get_provider_capacity(profile: Optional[str] = None) -> Dict[str, Any]:
    cache_key = (profile or "").strip().lower()
    now = time.monotonic()
    with _capacity_lock:
        snapshot = _capacity_snapshots.get(cache_key)
        if snapshot and (now - snapshot[0]) < _CAPACITY_TTL_S:
            payload = dict(snapshot[1])
            payload["cached"] = True
            return payload
        refresh = _capacity_inflight.get(cache_key)
        leader = refresh is None
        if leader:
            refresh = Future()
            _capacity_inflight[cache_key] = refresh
    if not leader:
        payload = dict(refresh.result())
        payload["cached"] = True
        return payload
    try:
        computed = _compute_provider_capacity(profile)
    except BaseException as exc:
        with _capacity_lock:
            _capacity_inflight.pop(cache_key, None)
            refresh.set_exception(exc)
        raise
    with _capacity_lock:
        _capacity_snapshots[cache_key] = (time.monotonic(), computed)
        _capacity_inflight.pop(cache_key, None)
        refresh.set_result(computed)
    return dict(computed)


@router.get("/api/analytics/provider-capacity")
async def get_provider_capacity(profile: Optional[str] = None):
    """Live provider allowance windows (Codex/OpenRouter/etc), TTL-cached 30s."""
    return await asyncio.to_thread(_get_provider_capacity, profile)
