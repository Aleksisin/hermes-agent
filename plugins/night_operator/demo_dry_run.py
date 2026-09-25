"""Safe tracked dry-run demo for the Night Operator plugin.

Creates a temporary Kanban database, completes synthetic batch members, invokes
the plugin-owned CLI without --write, and asserts that no task/comment is added.
It never reads live configuration, profiles, credentials, or production boards.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc
from hermes_constants import get_scratch_dir

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _plugin():
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    return importlib.import_module("plugins.night_operator")


@contextlib.contextmanager
def _isolated_demo_environment(home: Path):
    """Keep the synthetic board independent from delegation and live Kanban context."""
    values = {
        "HERMES_HOME": str(home),
        "HERMES_KANBAN_HOME": str(home),
        "HERMES_KANBAN_DB": str(home / "kanban.db"),
        "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_WORKSPACES_ROOT": str(home / "workspaces"),
        "HERMES_DELEGATED_CHILD_CONTEXT": "",
        "HERMES_KANBAN_TASK": "",
        "HERMES_KANBAN_RUN_ID": "",
        "HERMES_KANBAN_CLAIM_LOCK": "",
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _create_and_complete(
    conn,
    *,
    title: str,
    batch: str | None,
    expected_ids: list[str] | None = None,
    parents: tuple[str, ...] = (),
) -> str:
    task_id = kanban_db.create_task(
        conn,
        title=title,
        body="synthetic dry-run handoff",
        assignee="worker-a",
        created_by="worker-a",
        initial_status="running",
        parents=parents,
        board="default",
    )
    if not kanban_db.claim_task(conn, task_id, claimer="worker-a"):
        raise RuntimeError(f"failed to claim synthetic task {task_id}")
    metadata = (
        {
            "operator_batch": batch,
            "operator_expected_ids": expected_ids,
            "operator_group_ready": True,
        }
        if batch is not None
        else {}
    )
    if not kanban_db.complete_task(conn, task_id, summary="synthetic dry-run handoff", metadata=metadata):
        raise RuntimeError(f"failed to complete synthetic task {task_id}")
    return task_id


def main() -> int:
    plugin = _plugin()
    with tempfile.TemporaryDirectory(
        prefix="night-operator-demo-", dir=get_scratch_dir(prune=False),
    ) as tmp:
        home = Path(tmp)
        with _isolated_demo_environment(home):
            return _run_demo(plugin, home)


def _run_demo(plugin, home: Path) -> int:
    db_path = home / "kanban.db"
    kanban_db.init_db(db_path=db_path)
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  entries:\n"
        "    night-operator:\n"
        "      settings:\n"
        "        enabled: true\n"
        "        dry_run: true\n"
        "        board: default\n"
        "        max_items: 20\n",
        encoding="utf-8",
    )

    with kbc.connect_closing(db_path=db_path) as conn:
        parent = _create_and_complete(
            conn, title="synthetic parent", batch="demo-batch"
        )
        child = _create_and_complete(
            conn,
            title="synthetic child",
            batch="demo-batch",
            expected_ids=[parent, "pending-placeholder"],
            parents=(parent,),
        )
        # Replace the placeholder with the real child ID in the parent's
        # latest run metadata. This keeps both members unambiguous without
        # mutating task history after completion.
        run = kanban_db.latest_run(conn, parent)
        metadata = dict(run.metadata or {})
        metadata["operator_expected_ids"] = [parent, child]
        metadata["operator_group_ready"] = True
        with kanban_db.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (json.dumps(metadata, sort_keys=True), run.id),
            )
        _create_and_complete(conn, title="ambiguous task", batch=None)
        before_count = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        before_comments = int(conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0])

    parser = argparse.ArgumentParser()
    plugin.register_cli(parser)
    args = parser.parse_args(
        ["sweep", "--board", "default", "--home", str(home), "--json"]
    )
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = plugin.night_operator_command(args)
    payload = json.loads(stdout.getvalue())

    with kbc.connect_closing(db_path=db_path) as conn:
        after_count = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        after_comments = int(conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0])

    if exit_code != 0:
        raise RuntimeError(f"dry-run returned exit code {exit_code}")
    if payload.get("dry_run") is not True or payload.get("ok") is not True:
        raise RuntimeError(f"dry-run payload is not fail-closed: {payload!r}")
    if before_count != after_count or before_comments != after_comments:
        raise RuntimeError(
            f"dry-run mutated temporary board: tasks {before_count}->{after_count}, "
            f"comments {before_comments}->{after_comments}"
        )

    print(
        json.dumps(
            {
                "ok": True,
                "board": "default",
                "dry_run": True,
                "writes": 0,
                "task_count": after_count,
                "comment_count": after_comments,
                "outcome_count": payload.get("count"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
