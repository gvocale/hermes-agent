"""Provider capacity snapshot: profile scope, coalescing, and unavailable rows."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import threading

from hermes_cli.web_routers import analytics as analytics_mod


def _win(label="Session", pct=10.0):
    return SimpleNamespace(label=label, used_percent=pct, reset_at=datetime(2026, 9, 6, tzinfo=timezone.utc), detail=None)


def test_provider_capacity_coalesces_inflight_and_ttl(monkeypatch):
    analytics_mod.reset_provider_capacity_cache()
    calls = {"n": 0}

    def fake_fetch(provider, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(
            provider=provider,
            windows=(_win(),),
            fetched_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
            unavailable_reason=None,
            title="Account limits",
            plan=None,
            details=(),
        )

    monkeypatch.setattr(analytics_mod, "_list_capacity_providers", lambda profile=None: ["openai-codex"])
    monkeypatch.setattr("agent.account_usage.fetch_account_usage", fake_fetch)

    a = analytics_mod._get_provider_capacity()
    b = analytics_mod._get_provider_capacity()
    assert calls["n"] == 1
    assert a["cached"] is False
    assert b["cached"] is True
    assert a["providers"][0]["id"] == "openai-codex"
    assert a["providers"][0]["windows"][0]["used_percent"] == 10.0


def test_provider_capacity_keeps_unavailable_providers(monkeypatch):
    analytics_mod.reset_provider_capacity_cache()

    def fake_fetch(provider, **kwargs):
        return SimpleNamespace(
            provider=provider,
            windows=(),
            fetched_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
            unavailable_reason="auth failed",
            title="Account limits",
            plan=None,
            details=(),
        )

    monkeypatch.setattr(analytics_mod, "_list_capacity_providers", lambda profile=None: ["anthropic"])
    monkeypatch.setattr("agent.account_usage.fetch_account_usage", fake_fetch)

    payload = analytics_mod._get_provider_capacity()
    row = payload["providers"][0]
    assert row["id"] == "anthropic"
    assert row["windows"] == []
    assert row["unavailable_reason"] == "auth failed"


def test_capacity_provider_discovery_includes_absolute_only_sessions(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("gateway", source="slack", model="gpt-5.4")
    db.update_token_counts(
        "gateway", input_tokens=10, output_tokens=2, model="gpt-5.4",
        billing_provider="openai-codex", absolute=True,
    )
    real_close = db.close
    db.close = lambda: None
    monkeypatch.setattr(
        "hermes_cli.web_server_sessions._open_session_db_for_profile",
        lambda profile=None, read_only=True: db,
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    try:
        assert analytics_mod._list_capacity_providers() == ["openai-codex"]
    finally:
        db.close = real_close
        db.close()


def test_provider_capacity_cache_isolated_by_profile(monkeypatch):
    analytics_mod.reset_provider_capacity_cache()
    calls = []
    monkeypatch.setattr(analytics_mod, "_list_capacity_providers", lambda profile=None: [profile or "current"])
    monkeypatch.setattr(
        analytics_mod, "_fetch_one_capacity",
        lambda provider, profile=None: calls.append((provider, profile)) or {
            "id": provider, "windows": [], "fetched_at": None,
            "unavailable_reason": None, "plan": None, "details": [],
        },
    )

    worker = analytics_mod._get_provider_capacity("worker")
    current = analytics_mod._get_provider_capacity(None)
    worker_cached = analytics_mod._get_provider_capacity("worker")

    assert worker["providers"][0]["id"] == "worker"
    assert current["providers"][0]["id"] == "current"
    assert worker_cached["cached"] is True
    assert calls == [("worker", "worker"), ("current", None)]


def test_provider_capacity_fetch_loads_credentials_in_selected_profile(monkeypatch):
    active = {"profile": None}

    @contextmanager
    def profile_scope(profile):
        active["profile"] = profile
        try:
            yield
        finally:
            active["profile"] = None

    def fake_fetch(provider):
        assert provider == "anthropic"
        assert active["profile"] == "worker"
        return None

    monkeypatch.setattr(analytics_mod, "_config_profile_scope", profile_scope)
    monkeypatch.setattr("agent.account_usage.fetch_account_usage", fake_fetch)

    row = analytics_mod._fetch_one_capacity("anthropic", "worker")
    assert row["unavailable_reason"] == "no snapshot"


def test_provider_capacity_concurrent_callers_share_one_refresh(monkeypatch):
    analytics_mod.reset_provider_capacity_cache()
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def compute(profile=None):
        calls["n"] += 1
        entered.set()
        assert release.wait(2)
        return {"providers": [], "fetched_at": 1.0, "cached": False}

    monkeypatch.setattr(analytics_mod, "_compute_provider_capacity", compute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(analytics_mod._get_provider_capacity, "worker")
        assert entered.wait(2)
        second = pool.submit(analytics_mod._get_provider_capacity, "worker")
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert calls["n"] == 1
    assert sorted(result["cached"] for result in results) == [False, True]


def test_provider_capacity_sanitizes_fetch_exceptions(monkeypatch):
    def fake_fetch(provider):
        raise RuntimeError("request failed for https://private.example/token")

    monkeypatch.setattr("agent.account_usage.fetch_account_usage", fake_fetch)
    row = analytics_mod._fetch_one_capacity("anthropic")

    assert row["unavailable_reason"] == "capacity unavailable"
    assert "private.example" not in row["unavailable_reason"]
