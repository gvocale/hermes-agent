"""``gateway.delegation_completion_wake`` on the route every started gateway uses.

Each test runs one scenario of ``fixtures/delegation_wake_authority_peer.py`` in its own process with its own
``HERMES_HOME``: a ``GatewayRunner`` with a real session authority (``initialize_session_authority``), a real
``APIServerAdapter`` on a real port, real HTTP turns (the wake is the production self-post to the adapter's own
``/v1/chat/completions``), the real durable delegation ledger and the real watcher loop. Faked: the model (a loopback
HTTP peer), the finished helper (its completion event is pushed after a real ledger dispatch), and, where a test's
docstring in the fixture says so, one trigger (the owner retiring, a registered prompt). Waits are shortened with the
watcher interval and a fixed retry backoff (production: 4 s doubling to 60 s); the delivery-attempt limit (8) and
every other piece of the attempt logic are the real ones, so a busy turn long enough to refuse more than 8 retries
is a 10-minute busy parent in production terms (4+8+16+32+60... s of backoff).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.run_notifications import INTERNAL_NOTIFICATION_FOOTER
from session_rules.turns import AUTOMATED_NOTICE

FIXTURE = Path(__file__).parent / "fixtures" / "delegation_wake_authority_peer.py"
REPO = Path(__file__).resolve().parents[2]


def run_scenario(tmp_path, name, **env_extra):
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir()
    state.mkdir()
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ", "SYSTEMROOT") if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state), PYTHONPATH=str(REPO), **env_extra)
    result = subprocess.run([sys.executable, str(FIXTURE), name], cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=170, check=False)
    assert result.returncode == 0, result.stdout[-3000:] + "\n" + result.stderr[-3000:]
    return json.loads((state / "receipt.json").read_text())


def test_idle_session_is_woken_once_with_the_notice_wording(tmp_path):
    out = run_scenario(tmp_path, "idle")
    assert len(out["posts"]) == 1 and out["posts"][0]["at_most_once"] is True
    lines = out["wake_text"].split("\n")
    assert lines[0].startswith("[ASYNC DELEGATION") and lines[1] == AUTOMATED_NOTICE
    assert out["wake_text"].endswith(INTERNAL_NOTIFICATION_FOOTER)
    assert "the helper finished" in out["wake_text"]
    assert out["delivery_rows"] == 0
    assert [r["role"] for r in out["rows"][-2:]] == ["user", "assistant"] and out["rows"][-1]["text"] == "WOKEN_ACK"
    assert sum("[ASYNC DELEGATION" in call for call in out["model_calls"]) == 1  # the model was shown it once
    assert out["ledger"]["delivery_state"] == "delivered" and out["ledger"]["delivery_attempts"] == 1


def test_setting_off_keeps_the_delivery_row_and_starts_no_turn(tmp_path):
    out = run_scenario(tmp_path, "idle", WAKE="0")
    assert out["posts"] == [] and out["wake_setting"] is False
    assert out["delivery_rows"] == 1
    assert not any("[ASYNC DELEGATION" in call for call in out["model_calls"])
    assert out["ledger"]["delivery_state"] == "delivered"


@pytest.mark.parametrize("wake", ["1", "0"], ids=["setting-on", "setting-off"])
def test_a_busy_parent_is_not_woken_on_top_and_gets_its_result_after_the_turn(tmp_path, wake):
    """Fix 4. The turn is held for 5 s while the retry backoff is 0.3 s: more than the 8 delivery attempts a
    completion has are refused during the turn. Before the fix the ledger dropped the result after the 8th."""
    out = run_scenario(tmp_path, "busy", WAKE=wake, BACKOFF="0.3", HOLD="5")
    assert out["mid"]["posts_during_turn"] == 0 and out["mid"]["model_calls_during_turn"] == 0
    assert out["mid"]["ledger_during_turn"]["delivery_state"] == "pending"
    assert out["mid"]["ledger_during_turn"]["delivery_attempts"] <= 1  # a refused retry is refunded, not spent
    assert out["claims"] > 8, f"only {out['claims']} claims: the turn was too short to show the attempt limit"
    assert out["ledger"]["delivery_state"] == "delivered"
    if wake == "1":
        assert out["wakes"] == 1 and out["delivery_rows"] == 0 and len(out["posts"]) == 1
    else:
        assert out["wakes"] == 0 and out["delivery_rows"] == 1 and out["posts"] == []


def test_a_turn_on_another_session_does_not_block_the_wake(tmp_path):
    """Fix 1: on the authority route the request slot of every in-flight turn stays held for the whole
    request; one session's turn must not turn every other session's wake into a row."""
    out = run_scenario(tmp_path, "other_session")
    assert out["b_turn_still_running"] is True
    assert out["wakes"] == 1 and out["delivery_rows"] == 0 and len(out["posts"]) == 1
    assert out["pending"] >= 1 and out["authority_turns"] >= 1  # the other turn really held a slot


def test_a_503_admission_queued_is_delivered_and_shows_once(tmp_path):
    """Fix 2, over real HTTP: the authority's own 503 admission_queued answer."""
    out = run_scenario(tmp_path, "queued")
    assert out["wake_returned"] is True
    assert out["delivery_rows"] == 0
    assert len(out["posts"]) == 1


def test_a_pending_prompt_for_a_person_gets_no_wake(tmp_path):
    out = run_scenario(tmp_path, "prompt")
    for kind in ("clarify", "approval"):
        got = out[kind]
        assert got["posts"] == 0 and got["wakes"] == 0 and got["model_calls_after_hello"] == 0, kind
        assert got["delivery_rows"] == 1 and got["ledger"]["delivery_state"] == "delivered", kind


def test_a_wake_turn_whose_model_call_fails_keeps_the_result_as_a_row(tmp_path):
    out = run_scenario(tmp_path, "wake_fails")
    assert out["wakes"] == 1  # the turn ran (and failed): its user row is in the transcript
    assert out["delivery_rows"] == 1  # so the client can still pick the result up
    assert "not processed" in out["rows"][-2]["text"] and out["ledger"]["delivery_state"] == "delivered"
    assert len(out["posts"]) == 1  # and no second turn was sent


def test_a_wake_turn_longer_than_the_timeout_is_sent_once_with_no_row(tmp_path):
    out = run_scenario(tmp_path, "timeout", WAKE_TIMEOUT="1")
    assert out["posts_after_timeout"] == 1 and len(out["calls_after_timeout"]) == 1
    assert out["wake_model_calls"] == 1 and out["wakes"] == 1 and out["delivery_rows"] == 0
    assert out["ledger"]["delivery_state"] == "delivered"
