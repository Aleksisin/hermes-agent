"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48






def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True


# ---------------------------------------------------------------------------
# stranded_in_ready — the lane the dispatcher refused to spawn into (#21582)
#
# A card also sits ready past the threshold when its assignee's lane is at
# ``kanban.max_in_progress_per_profile``: the tick defers it as
# ``skipped_per_profile_capped`` and spawns it on the first free slot. That is
# normal dispatch behaviour, not a broken assignee — so the rule names the cap,
# the cards holding the lane and their ages instead of sending the operator to
# hunt a typo and a dead worker pool.
# ---------------------------------------------------------------------------


def _write_lane_cap(kanban_home, cap):
    """Put ``kanban.max_in_progress_per_profile`` in the profile's config file
    (or remove it) — the single knob the dispatcher and the rule both read."""
    path = kanban_home / "config.yaml"
    if cap is None:
        if path.exists():
            path.unlink()
        return
    path.write_text(
        f"""
kanban:
  max_in_progress_per_profile: {cap}
""".lstrip(),
        encoding="utf-8",
    )


def _claimed(conn, title, assignee):
    """Create + claim through the dispatcher's own claim path: a live run now
    holds that profile's lane."""
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    assert kb.claim_task(conn, task_id) is not None
    return task_id


def _stranded_diags(conn, task_id, *, now):
    """Diagnostics for one task exactly as the CLI/dashboard compute them: the
    runtime config from ``kanban.*`` plus the live lane snapshot."""
    from hermes_cli.config import load_config

    cfg = dict(kd.config_from_runtime_config(load_config()))
    cfg["lane_state"] = kd.lane_saturation_snapshot(conn, now=now)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    events = list(conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)))
    runs = list(conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)))
    diags = kd.compute_task_diagnostics(row, events, runs, now=now, config=cfg)
    return [d for d in diags if d.kind == "stranded_in_ready"]


def test_stranded_names_saturated_lane_and_holders(kanban_home):
    """cap=2, two live runs of the assignee, a third card past the threshold →
    the text names the saturation and both holders (ids + ages), and drops the
    three generic causes it would otherwise guess from."""
    _write_lane_cap(kanban_home, 2)
    conn = kbc.connect()
    try:
        holders = [_claimed(conn, f"lane holder {i}", "ibf-coder") for i in range(2)]
        # Distinct start times, so "oldest holder first" is a real contract and not
        # a tie between two cards created inside the same second (which the payload
        # would then order by task id).
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = started_at - 600 WHERE id = ?",
                         (holders[0],))
        waiting = kb.create_task(conn, title="waiting", assignee="ibf-coder")
        now = int(time.time()) + 45 * 60
        stranded = _stranded_diags(conn, waiting, now=now)
    finally:
        conn.close()

    assert len(stranded) == 1
    d = stranded[0]
    assert d.data["lane_capped"] is True
    assert d.data["lane_cap"] == 2
    assert d.data["lane_running"] == 2
    # Oldest first, with the backdated holder ahead by the 600s we removed.
    assert [h["id"] for h in d.data["lane_holders"]] == holders
    assert (d.data["lane_holders"][0]["age_seconds"]
            - d.data["lane_holders"][1]["age_seconds"]) >= 600
    # The holders have been running for the whole 45 min the card has waited.
    assert all(h["age_seconds"] > 30 * 60 for h in d.data["lane_holders"])
    for holder_id in holders:
        assert holder_id in d.detail
    assert "max_in_progress_per_profile" in d.detail
    # The generic causes are for a FREE lane — not this one.
    assert "misspelled" not in d.detail
    assert "Common causes" not in d.detail


def test_stranded_free_lane_keeps_generic_causes(kanban_home):
    """Negative control: cap=2 but only ONE holder of this profile (the second
    live run belongs to another profile) → the lane is free, so the three
    original causes survive word for word."""
    _write_lane_cap(kanban_home, 2)
    conn = kbc.connect()
    try:
        _claimed(conn, "holder", "ibf-coder")
        _claimed(conn, "other lane", "other-profile")
        waiting = kb.create_task(conn, title="waiting", assignee="ibf-coder")
        now = int(time.time()) + 45 * 60
        stranded = _stranded_diags(conn, waiting, now=now)
    finally:
        conn.close()

    assert len(stranded) == 1
    d = stranded[0]
    assert not d.data.get("lane_capped")
    assert "misspelled" in d.detail
    assert "profile was deleted" in d.detail
    assert "external worker pool" in d.detail


def test_lane_snapshot_is_empty_without_a_cap(kanban_home):
    """No ``kanban.max_in_progress_per_profile`` → no lane signal at all: the
    dispatcher defers nothing without a cap, so a busy lane cannot explain a
    stranded card and the generic causes stay in charge."""
    _write_lane_cap(kanban_home, None)
    conn = kbc.connect()
    try:
        _claimed(conn, "holder", "ibf-coder")
        _claimed(conn, "holder 2", "ibf-coder")
        assert kd.lane_saturation_snapshot(conn) == {}
        waiting = kb.create_task(conn, title="waiting", assignee="ibf-coder")
        now = int(time.time()) + 45 * 60
        stranded = _stranded_diags(conn, waiting, now=now)
    finally:
        conn.close()

    assert len(stranded) == 1
    d = stranded[0]
    assert not d.data.get("lane_capped")
    assert "misspelled" in d.detail
