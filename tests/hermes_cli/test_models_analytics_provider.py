"""Dashboard Models analytics must split usage by the billed provider.

A session row stores one (model, billing_provider) pair. Mid-session /model
switches and aggregator routes live in session_model_usage. Grouping the
sessions table therefore attributes every token to the last/main provider.
"""
from __future__ import annotations

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _analytics(monkeypatch, db):
    from hermes_cli.web_routers import analytics as analytics_mod

    monkeypatch.setattr(
        "hermes_cli.web_server_sessions._open_session_db_for_profile",
        lambda profile=None, read_only=True: db,
    )
    # _get_models_analytics closes the db; wrap so the fixture stays usable.
    real_close = db.close
    db.close = lambda: None
    try:
        return analytics_mod._get_models_analytics(days=30)
    finally:
        db.close = real_close


def test_models_analytics_splits_providers_from_session_model_usage(db, monkeypatch):
    db.create_session("s1", source="cli", model="gpt-5.4")
    db.update_token_counts(
        "s1",
        input_tokens=1000,
        output_tokens=100,
        model="gpt-5.4",
        billing_provider="openai-codex",
        api_call_count=1,
    )
    db.update_token_counts(
        "s1",
        input_tokens=4000,
        output_tokens=400,
        model="gpt-5.4",
        billing_provider="custom",
        api_call_count=2,
    )

    payload = _analytics(monkeypatch, db)
    by_key = {(row["model"], row["provider"]): row for row in payload["models"]}

    assert ("gpt-5.4", "openai-codex") in by_key
    assert ("gpt-5.4", "custom") in by_key
    assert by_key[("gpt-5.4", "openai-codex")]["input_tokens"] == 1000
    assert by_key[("gpt-5.4", "custom")]["input_tokens"] == 4000
    # Must not dump the whole session onto the sessions-row provider.
    openai_only = [r for r in payload["models"] if r["provider"] == "openai-codex"]
    assert sum(r["input_tokens"] for r in openai_only) == 1000


def _usage(monkeypatch, db):
    from hermes_cli.web_routers import analytics as analytics_mod

    monkeypatch.setattr(
        "hermes_cli.web_server_sessions._open_session_db_for_profile",
        lambda profile=None, read_only=True: db,
    )
    real_close = db.close
    db.close = lambda: None
    try:
        return analytics_mod._get_usage_analytics(days=30)
    finally:
        db.close = real_close


def test_usage_analytics_splits_providers_from_session_model_usage(db, monkeypatch):
    db.create_session("s1", source="cli", model="gpt-5.4")
    db.update_token_counts(
        "s1",
        input_tokens=1000,
        output_tokens=100,
        model="gpt-5.4",
        billing_provider="openai-codex",
        api_call_count=1,
    )
    db.update_token_counts(
        "s1",
        input_tokens=4000,
        output_tokens=400,
        model="gpt-5.4",
        billing_provider="custom",
        api_call_count=2,
    )

    payload = _usage(monkeypatch, db)
    by_key = {(row.get("model"), row.get("provider") or row.get("billing_provider")): row for row in payload["by_model"]}
    assert ("gpt-5.4", "custom") in by_key
    assert by_key[("gpt-5.4", "custom")]["input_tokens"] == 4000
    assert sum(r["input_tokens"] for r in payload["by_model"] if (r.get("provider") or r.get("billing_provider")) == "openai-codex") == 1000


def test_usage_analytics_keeps_absolute_only_session_usage(db, monkeypatch):
    db.create_session("absolute", source="slack", model="gpt-5.4")
    db.update_token_counts(
        "absolute",
        input_tokens=123,
        output_tokens=45,
        model="gpt-5.4",
        billing_provider="openai-codex",
        api_call_count=2,
        absolute=True,
    )

    payload = _usage(monkeypatch, db)
    row = next(
        row for row in payload["by_model"]
        if row["model"] == "gpt-5.4" and row["provider"] == "openai-codex"
    )
    assert row["input_tokens"] == 123
    assert row["output_tokens"] == 45
    assert row["api_calls"] == 2


def test_models_analytics_counts_physical_sessions_once_across_aux_tasks(db, monkeypatch):
    db.create_session("shared", source="cli", model="gpt-5.4")
    db.update_token_counts(
        "shared",
        input_tokens=100,
        output_tokens=10,
        model="gpt-5.4",
        billing_provider="openai-codex",
        api_call_count=1,
    )
    db.record_auxiliary_usage(
        "shared",
        "compression",
        model="gpt-5.4",
        billing_provider="openai-codex",
        input_tokens=20,
    )
    db.record_auxiliary_usage(
        "shared",
        "vision",
        model="gpt-5.4",
        billing_provider="openai-codex",
        input_tokens=30,
    )

    payload = _analytics(monkeypatch, db)
    row = next(
        row for row in payload["models"]
        if row["model"] == "gpt-5.4" and row["provider"] == "openai-codex"
    )
    assert row["sessions"] == 1
    assert row["input_tokens"] == 150


def test_models_analytics_merges_aux_usage_into_existing_provider_card(db, monkeypatch):
    db.create_session("s1", source="cli", model="shared-model")
    db.update_token_counts(
        "s1", input_tokens=100, output_tokens=10, model="shared-model",
        billing_provider="anthropic", api_call_count=1,
    )
    db.record_auxiliary_usage(
        "s1", task="compression", model="shared-model", billing_provider="anthropic",
        input_tokens=20, output_tokens=2, api_call_count=1,
    )

    payload = _analytics(monkeypatch, db)
    matching = [
        row for row in payload["models"]
        if row["model"] == "shared-model" and row["provider"] == "anthropic"
    ]
    assert len(matching) == 1
    assert matching[0]["input_tokens"] == 120
    assert matching[0]["output_tokens"] == 12
