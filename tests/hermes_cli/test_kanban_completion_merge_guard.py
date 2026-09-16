"""A card must not close silently while its work is still unlanded.

Observed class (IBF board, 2026-09-16): a dispatcher-driven card reached
``done`` while its ``wt/<task-id>`` branch was never merged into the target
tree. The worktree teardown preserves such a worktree (unpushed commits), so
the commits survived — but nothing in the completion path said so, and the
only way to notice was a hand-run ``git rev-list --count HEAD..wt/<task-id>``.

The completion path now carries a warning-only verdict: the card's branch is
either reachable from another local branch (landed in the target tree) or the
card declares ``metadata["close_without_merge"]`` with a reason. Anything
else is recorded on the completion event and in the run metadata — never a
refusal, because a legitimate "closed without merge" close must stay possible.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw

WARNING_KIND = "completion_unmerged_branch"


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _worktree_task(conn, repo: Path, title: str = "wt-task") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title, assignee="worker")
    wt = repo / ".worktrees" / tid
    kbw._ensure_git_worktree(repo, wt, f"wt/{tid}")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


def _commit_in(wt: Path, name: str, text: str = "work\n") -> None:
    _git("-C", str(wt), "config", "user.email", "w@example.com")
    _git("-C", str(wt), "config", "user.name", "w")
    (wt / name).write_text(text, encoding="utf-8")
    _git("-C", str(wt), "add", name)
    _git("-C", str(wt), "commit", "-m", f"card work: {name}")


def _start(conn, tid: str) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _completed_event(conn, tid: str) -> dict | None:
    """Payload of the last ``completed`` event, or None."""
    for event in kb.list_events(conn, tid):
        if event.kind == "completed":
            payload = event.payload
            return json.loads(payload) if isinstance(payload, str) else payload
    return None


def _warning_events(conn, tid: str) -> list[dict]:
    out = []
    for event in kb.list_events(conn, tid):
        if event.kind != WARNING_KIND:
            continue
        payload = event.payload
        out.append(json.loads(payload) if isinstance(payload, str) else payload)
    return out


def _run_metadata(conn, tid: str) -> dict:
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    if not row or not row["metadata"]:
        return {}
    return json.loads(row["metadata"])


# ---------------------------------------------------------------------------
# The unlanded work is reported
# ---------------------------------------------------------------------------


def test_unmerged_branch_warns_on_completion(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _commit_in(wt, "feature.txt")
        _start(conn, tid)
        assert kb.complete_task(
            conn, tid, summary="done", metadata={"changed_files": ["feature.txt"]}
        )

        warnings = _warning_events(conn, tid)
        assert warnings, "an unmerged card branch must be warned about"
        assert warnings[-1]["branch"] == f"wt/{tid}"
        assert warnings[-1]["unmerged_commits"] >= 1
        assert warnings[-1]["dirty"] is False

        run_meta = _run_metadata(conn, tid)
        assert run_meta["unmerged_branch"]["branch"] == f"wt/{tid}"
        assert run_meta["unmerged_branch"]["unmerged_commits"] >= 1
        # the worker's own metadata is enriched, never clobbered
        assert run_meta["changed_files"] == ["feature.txt"]

        payload = _completed_event(conn, tid)
        assert payload["unmerged_branch"]["branch"] == f"wt/{tid}"


def test_warning_text_names_the_branch_and_the_way_out(
    kanban_home: Path, repo: Path
) -> None:
    """The warning must be actionable: which branch, and how to declare a close."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _commit_in(wt, "feature.txt")
        _start(conn, tid)
        assert kb.complete_task(conn, tid, summary="done")
        warning = _run_metadata(conn, tid)["unmerged_branch"]["warning"]
        assert f"wt/{tid}" in warning
        assert kb.CLOSE_WITHOUT_MERGE_KEY in warning


def test_dirty_worktree_warns_without_commits(kanban_home: Path, repo: Path) -> None:
    """Uncommitted work is unlanded work too — the teardown preserves it."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        _start(conn, tid)
        assert kb.complete_task(conn, tid, summary="done")

        warnings = _warning_events(conn, tid)
        assert warnings and warnings[-1]["dirty"] is True
        assert warnings[-1]["unmerged_commits"] == 0


# ---------------------------------------------------------------------------
# Reverse class: landed / behind / declared — the guard stays silent
# ---------------------------------------------------------------------------


def test_merged_branch_is_silent(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _commit_in(wt, "feature.txt")
        _git("-C", str(repo), "merge", "--no-ff", "-m", "merge card", f"wt/{tid}")
        _start(conn, tid)
        assert kb.complete_task(conn, tid, summary="merged and done")

        assert _warning_events(conn, tid) == []
        assert "unmerged_branch" not in _run_metadata(conn, tid)
        assert "unmerged_branch" not in (_completed_event(conn, tid) or {})


def test_branch_behind_the_tree_is_silent(kanban_home: Path, repo: Path) -> None:
    """A card branch older than the target tree is landed, not stranded."""
    with kbc.connect_closing() as conn:
        tid, _wt = _worktree_task(conn, repo)
        (repo / "later.txt").write_text("moved on\n", encoding="utf-8")
        _git("-C", str(repo), "add", "later.txt")
        _git("-C", str(repo), "commit", "-m", "tree moves on")
        _start(conn, tid)
        assert kb.complete_task(conn, tid, summary="nothing to land")

        assert _warning_events(conn, tid) == []
        assert "unmerged_branch" not in _run_metadata(conn, tid)


def test_declared_close_without_merge_is_silent(kanban_home: Path, repo: Path) -> None:
    """An explicit decision closes the class: silence, but the reason is kept."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _commit_in(wt, "superseded.txt")
        _start(conn, tid)
        assert kb.complete_task(
            conn, tid, summary="superseded by the tree",
            metadata={"close_without_merge": "superseded by main (version bump)"},
        )

        assert _warning_events(conn, tid) == []
        run_meta = _run_metadata(conn, tid)
        assert "unmerged_branch" not in run_meta
        assert run_meta["close_without_merge"] == "superseded by main (version bump)"


def test_blank_close_without_merge_declaration_still_warns(
    kanban_home: Path, repo: Path
) -> None:
    """A declaration without a reason is not a decision — the warning stands."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _commit_in(wt, "feature.txt")
        _start(conn, tid)
        assert kb.complete_task(
            conn, tid, summary="done", metadata={"close_without_merge": "   "}
        )
        assert _warning_events(conn, tid), "a blank reason must not buy silence"


def test_non_worktree_workspace_is_silent(kanban_home: Path, tmp_path: Path) -> None:
    """Scratch cards have no branch to check — the guard must not invent one."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="scratch-task", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?",
                (str(scratch), tid),
            )
        _start(conn, tid)
        assert kb.complete_task(conn, tid, summary="done")
        assert _warning_events(conn, tid) == []
