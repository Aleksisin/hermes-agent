"""Native, fail-closed Night Operator for Hermes Kanban.

The plugin observes native Kanban lifecycle hooks and performs only bounded,
board-local work.  It never uses GUI/OCR, terminal, browser, credentials, or
arbitrary external communication.  A verification card is the only action
created from a completed batch; lifecycle outcomes use the native Kanban
transitions and durable comments/events.

The module is deliberately usable as a small library for dry-run integration
tests before it is enabled in a profile.  Configuration is fail-closed by
default: the live hook callbacks do nothing unless the profile explicitly sets
``enabled: true`` and ``dry_run: false``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)
_PLANNED_RECONCILIATION_TASKS: dict[str, float] = {}
_PLANNED_RECONCILIATION_LAST_RUN: dict[str, float] = {}
_PLANNED_RECONCILIATION_LOCK = threading.RLock()


# Public namespace is intentionally explicit.  PluginContext settings live at
# plugins.entries.<plugin-id>.settings; the same names are used in the example
# configuration shipped with this plugin.
DEFAULTS: Mapping[str, Any] = {
    "enabled": False,
    "dry_run": True,
    "board": "default",
    "review_group_key": "operator_batch",
    "max_review_rounds": 2,
    "reconciliation_interval_seconds": 300,
    "escalation_channel": None,
    "max_items": 100,
    "enforce_tools": True,
    "operator_profile": "night-operator",
}


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    dry_run: bool = True
    board: str = "default"
    review_group_key: str = "operator_batch"
    max_review_rounds: int = 2
    reconciliation_interval_seconds: int = 300
    escalation_channel: str | None = None
    max_items: int = 100
    operator_profile: str = "night-operator"
    enforce_tools: bool = True


@dataclass
class ReviewEvidence:
    verification_task_id: str
    operator_profile: str
    implementation_profiles: Sequence[str]
    verified_parent_ids: Sequence[str]
    evidence: Sequence[str]
    artifacts: Sequence[str] = ()
    residual_risk: Sequence[str] = ()
    batch_id: str | None = None
    review_round: int = 1
    max_review_rounds: int = 2


@dataclass
class Outcome:
    terminal_state: str
    requires_human: bool = False
    reason: str = ""
    question: str = ""
    options: Sequence[str] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the caller-facing redaction boundary for every Outcome."""
        self.reason = _redact(self.reason, 2000)
        self.question = _redact(self.question, 2000)
        self.options = tuple(_redact(item, 2000) for item in self.options)
        self.metadata = _redact_structure(self.metadata)

    @property
    def outcome(self) -> str:
        """Compatibility name used by hook callers and older tests."""
        return self.terminal_state


class Spy:
    """Small external adapter used by unit tests; not used by production hooks."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> str:
        self.writes.append(kwargs)
        return str(kwargs.get("idempotency_key", "generated"))

    def notify(self, **payload: Any) -> None:
        self.notices.append(payload)


_OPERATOR_PREFIXES = ("operator-review:", "operator-remediation:", "operator-escalation:", "operator-decision:")
_INTERNAL_TITLE_PREFIXES = ("Night review:", "Remediation:", "Escalation:", "Operator decision required:")
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _text(value: Any, limit: int = 4000) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _redact(value: Any, limit: int = 4000) -> str:
    """Redact untrusted task text before it reaches a durable field or notice."""
    from hermes_cli import kanban_db

    return _text(kanban_db.redact_review_value(value), limit)


def _strict_bool(value: Any, default: bool) -> bool:
    """Read YAML booleans fail-closed; strings such as ``"false"`` are invalid."""
    if isinstance(value, bool):
        return value
    return default


def _profile_key(value: Any) -> str:
    """Normalize a profile identity for equality checks without exposing it."""
    return _text(value, 200).lower()


def _active_run_profile(conn: Any, task_id: str) -> str:
    """Return the profile owning the task's current run, or an empty value."""
    from hermes_cli import kanban_db

    task = kanban_db.get_task(conn, task_id)
    if task is None or task.current_run_id is None:
        return ""
    run = kanban_db.get_run(conn, task.current_run_id)
    return _profile_key(getattr(run, "profile", "")) if run is not None else ""


def _closed_run_profile(conn: Any, task_id: str, outcome: str) -> str:
    """Return the latest closed native run profile for a lifecycle outcome."""
    row = conn.execute(
        "SELECT profile FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL AND outcome = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (task_id, outcome),
    ).fetchone()
    return _profile_key(row["profile"]) if row is not None else ""


def _implementation_profile_for(
    conn: Any, *, verification_task_id: str, source_parent_id: str | None,
    source_status: str,
) -> str:
    """Resolve the implementer from native run history, never from caller text."""
    if source_status == "review":
        return _closed_run_profile(conn, verification_task_id, "review_requested")
    if source_status == "done" and source_parent_id:
        return _closed_run_profile(conn, source_parent_id, "completed")
    return ""


def _key_component(value: Any, limit: int = 1000) -> str:
    """Keep idempotency keys readable while excluding secret-like task text."""
    return _redact(value, limit)


def _durable_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 1_000_000) -> int:
    """Read bounded integer configuration without trusting string coercion."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, min(int(value), maximum))


def _durable_text(value: Any, limit: int = 1000) -> str:
    """Redact before using worker-controlled text in a task title or reason."""
    return _redact(value, limit)


def _safe_ids(value: Any, limit: int = 500) -> list[str]:
    """Normalize and redact identifiers before they enter durable metadata."""
    return [_durable_text(item, limit) for item in _ids(value)]


def _redact_structure(value: Any) -> Any:
    """Recursively redact caller-facing dict/list/tuple values at the public boundary."""
    if isinstance(value, Mapping):
        return {
            _redact(str(key), 500): _redact_structure(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_structure(item) for item in value)
    if isinstance(value, str):
        return _redact(value, 10_000)
    return value


_MAX_ARTIFACT_IDENTIFIERS = 100
_MAX_ARTIFACT_SOURCE_CARDS = 100
_MAX_ARTIFACT_RECORDS = 1_000
_MAX_ARTIFACT_IDENTITY_CHARS = 4_000


def _artifact_identifiers(value: Any) -> list[str]:
    """Keep raw artifact identities exact; normalization would create aliases."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > _MAX_ARTIFACT_IDENTITY_CHARS:
            return []
        result.append(item)
    return result


def _safe_exact_artifact_identifiers(value: Any) -> list[str] | None:
    """Return ordered raw identities only when public-boundary redaction is a no-op."""
    identities = _artifact_identifiers(value)
    if not identities:
        return None
    if any(_redact_structure(identity) != identity for identity in identities):
        return None
    return identities


def _bounded_native_parent_ids(
    conn: Any, verification_task_id: str,
) -> tuple[list[str] | None, str]:
    """Read at most 99 parents plus one overflow witness from native task links."""
    max_parents = _MAX_ARTIFACT_SOURCE_CARDS - 1
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id LIMIT ?",
        (verification_task_id, max_parents + 1),
    ).fetchall()
    if len(rows) > max_parents:
        return None, "artifact record verification exceeds its source-card bound"
    return [str(row["parent_id"]) for row in rows], ""


def _native_parent_claims(value: Any) -> list[str] | None:
    """Return ordered, unique, exact parent IDs without worker-text normalization."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    claims = list(value)
    if len(claims) > _MAX_ARTIFACT_SOURCE_CARDS - 1:
        return None
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > 200
        or _redact_structure(item) != item
        for item in claims
    ):
        return None
    if len(set(claims)) != len(claims):
        return None
    return claims


def _native_parent_audit(
    conn: Any, verification_task_id: str, claimed_parent_ids: Sequence[str],
) -> tuple[list[str] | None, str]:
    """Require exact claimed parent ownership to equal the native task-link graph."""
    native, bound_reason = _bounded_native_parent_ids(conn, verification_task_id)
    if native is None:
        return None, bound_reason
    claimed = _native_parent_claims(claimed_parent_ids)
    if claimed is None or len(claimed) != len(native) or set(claimed) != set(native):
        return None, "claimed verified parents do not exactly match native parent links"
    return native, ""


def _bounded_attachment_rows(
    conn: Any, task_id: str, limit: int,
) -> tuple[list[Any], bool]:
    """Read at most ``limit`` rows plus one overflow witness from native metadata."""
    rows = conn.execute(
        "SELECT id, task_id, filename, stored_path, size FROM task_attachments "
        "WHERE task_id = ? ORDER BY created_at ASC, id ASC LIMIT ?",
        (task_id, int(limit) + 1),
    ).fetchall()
    return rows, len(rows) > int(limit)


def _physical_artifact_sha256(
    rows: Sequence[Any], artifacts: Sequence[str], expected_sha256: Mapping[str, str], *, board: str,
) -> tuple[dict[str, str] | None, str]:
    """Hash native attachment blobs under the board-owned root, never worker paths."""
    from hermes_cli import kanban_db

    declared = set(_safe_exact_artifact_identifiers(artifacts) or ())
    if not declared:
        return None, "pass requires redaction-invariant exact artifacts"
    try:
        root = Path(kanban_db.attachments_root(board=board)).resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"native attachment root is unavailable: {type(exc).__name__}"
    expected: dict[str, str] = {}
    for identity, digest in expected_sha256.items():
        if not isinstance(identity, str) or identity not in declared or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            return None, "physical artifact digest is invalid or not declared"
        expected[identity] = str(digest)
    if set(expected) != declared:
        return None, "pass requires an expected SHA-256 for every physical artifact identity"
    observed: dict[str, str] = {}
    for identity, digest in expected.items():
        accepted = False
        for row in rows:
            if identity not in (row["filename"], row["stored_path"]):
                continue
            try:
                resolved = Path(str(row["stored_path"])).resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    continue
                data = resolved.read_bytes()
                if len(data) != int(row["size"]) or hashlib.sha256(data).hexdigest() != digest:
                    continue
            except (OSError, ValueError):
                continue
            observed[identity] = digest
            accepted = True
            break
        if not accepted:
            return None, "declared physical artifact failed native-root, regular-file, size, or SHA-256 verification"
    return observed, ""


_VERIFICATION_CHECKS: Mapping[str, Any] = {
    "json": lambda data: isinstance(json.loads(data.decode("utf-8")), object),
    "utf8-text": lambda data: bool(data.decode("utf-8").strip()),
    "sha256": lambda data: bool(re.fullmatch(r"[0-9a-f]{64}", hashlib.sha256(data).hexdigest())),
}


def _bounded_verification(
    requested: Any, rows: Sequence[Any], declared: Sequence[str], *, board: str,
) -> tuple[dict[str, Any] | None, str]:
    """Run only named in-process checks over native attachment blobs.

    A command string is never accepted as evidence: this plugin has no shell and
    no subprocess. The only authorized checks are the closed ``_VERIFICATION_CHECKS``
    table, applied to the blob of a native attachment row under the board-owned
    attachment root.
    """
    from hermes_cli import kanban_db

    if not isinstance(requested, (list, tuple)) or not requested:
        return None, "verification checks must be a non-empty list of named checks"
    checks: list[str] = []
    for item in requested:
        if not isinstance(item, str) or item not in _VERIFICATION_CHECKS:
            return None, "verification check is not in the closed allowlist"
        checks.append(item)
    if len(checks) != len(set(checks)):
        return None, "verification checks must not repeat"
    identities = _safe_exact_artifact_identifiers(declared)
    if identities is None:
        return None, "verification requires redaction-invariant exact artifacts"
    try:
        root = Path(kanban_db.attachments_root(board=board)).resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"native attachment root is unavailable: {type(exc).__name__}"
    results: list[dict[str, Any]] = []
    for identity in identities:
        outcome: dict[str, bool] = {}
        for row in rows:
            if identity not in (row["filename"], row["stored_path"]):
                continue
            try:
                resolved = Path(str(row["stored_path"])).resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    continue
                data = resolved.read_bytes()
                if len(data) != int(row["size"]):
                    continue
            except (OSError, ValueError):
                continue
            for check in checks:
                try:
                    outcome[check] = bool(_VERIFICATION_CHECKS[check](data))
                except (UnicodeDecodeError, ValueError, TypeError):
                    outcome[check] = False
            break
        if not outcome:
            return None, "verification check requires a native attachment blob under the board root"
        if not all(outcome.values()):
            return None, f"bounded verification failed for check(s): {', '.join(sorted(k for k, v in outcome.items() if not v))}"
        results.append({"artifact": identity, "checks": outcome})
    return {"verification_checks": results, "verification_check_names": checks}, ""


_DENIED_TOOLS: frozenset[str] = frozenset({
    "terminal", "process", "browser_use", "browser_exec", "computer_use",
    "vault_list", "vault_fill", "vault_unlock", "vault_enter_code", "vault_save_login",
    "web_search", "web_extract", "execute_code", "cronjob_manage", "todo_list",
})


def _operator_profile_active() -> str:
    """Resolve the live profile from native profile state, not from plugin text."""
    try:
        from hermes_cli import profiles
    except Exception:
        return ""
    for getter in ("get_active_profile_name", "get_active_profile"):
        try:
            value = getattr(profiles, getter)()
        except Exception:
            continue
        name = _text(value, 100)
        if name and name != "custom":
            return name
    return ""


def _on_pre_tool_call(
    ctx: Any, tool_name: str = "", args: Any = None, session_id: str = "", **_: Any,
) -> dict[str, Any] | None:
    """Fail-closed, profile-scoped denial of dangerous tools.

    Returns the native block directive only inside the operator profile: the
    operator reviews Kanban events, never pixels, and never holds a shell,
    browser, vault or network capability. The scope comes from the live profile
    resolved by the runtime (plus the configured operator profile), so a foreign
    profile such as ``ibf-operator`` is never restricted by this plugin. A
    profile that cannot be resolved fails open for safety of other profiles.
    """
    config = _config_from_context(ctx)
    if not config.enabled or not config.enforce_tools:
        return None
    live = _operator_profile_active()
    expected = _text(config.operator_profile, 100) or "night-operator"
    candidates = {name for name in (live, expected) if name}
    if not any(name.startswith(expected) for name in candidates):
        return None
    if live and not live.startswith(expected):
        # A foreign live profile wins: never deny tools outside the operator.
        return None
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if tool_name in _DENIED_TOOLS:
        return {
            "action": "block",
            "message": (
                f"night-operator policy denies tool {tool_name!r}: the operator reviews "
                "Kanban events without a shell, browser, vault, network or scheduler capability"
            ),
        }
    return None


def _verify_attachment_records(
    conn: Any,
    *,
    verification_task_id: str,
    artifacts: Sequence[str],
    source_parent_id: str | None = None,
    physical_sha256: Mapping[str, str] | None = None,
    verification_checks: Sequence[str] | None = None,
    board: str = "default",
) -> tuple[dict[str, Any] | None, str]:
    """Prove logical records and, when requested, exact native attachment blobs."""
    from hermes_cli import kanban_db

    artifact_ids = _safe_exact_artifact_identifiers(artifacts)
    if artifact_ids is None:
        return None, "pass requires redaction-invariant exact artifacts"
    if len(artifact_ids) > _MAX_ARTIFACT_IDENTIFIERS:
        return None, "artifact record verification exceeds its identifier bound"

    parent_ids, parent_bound_reason = _bounded_native_parent_ids(conn, verification_task_id)
    if parent_ids is None:
        return None, parent_bound_reason
    if source_parent_id:
        source_key = _durable_text(source_parent_id, 200)
        if source_key not in parent_ids:
            return None, "done source is not a native parent of the verification card"

    source_ids = _ids((verification_task_id, *parent_ids))
    if len(source_ids) > _MAX_ARTIFACT_SOURCE_CARDS:
        return None, "artifact record verification exceeds its source-card bound"

    identities: set[str] = set()
    matched_artifact_ids: set[str] = set()
    record_count = 0
    collected_rows: list[Any] = []
    for task_id in source_ids:
        if kanban_db.get_task(conn, task_id) is None:
            return None, "artifact verification source card is missing"
        remaining = _MAX_ARTIFACT_RECORDS - record_count
        rows, overflowed = _bounded_attachment_rows(conn, task_id, remaining)
        for attachment in rows:
            if overflowed:
                return None, "artifact record verification exceeds its record bound"
            record_count += 1
            collected_rows.append(attachment)
            for identity in (
                attachment["filename"],
                attachment["stored_path"],
            ):
                if (
                    isinstance(identity, str)
                    and identity
                    and len(identity) <= _MAX_ARTIFACT_IDENTITY_CHARS
                ):
                    identities.add(identity)
                    if identity in artifact_ids:
                        matched_artifact_ids.add(identity)

    if not all(artifact in identities for artifact in artifact_ids):
        return None, "declared artifact has no durable attachment record on the verification card or its native parents"
    if not matched_artifact_ids:
        return None, "declared artifact has no durable attachment record"

    audit: dict[str, Any] = {
        "artifact_record_presence": "logical_only",
        "artifact_record_count": len(matched_artifact_ids),
        "artifact_physical_presence": "unverified",
    }
    if physical_sha256 is not None:
        if not isinstance(physical_sha256, Mapping):
            return None, "physical artifact digests must be a mapping"
        observed, physical_reason = _physical_artifact_sha256(
            collected_rows, artifact_ids, physical_sha256, board=board,
        )
        if observed is None:
            return None, physical_reason
        audit.update(
            artifact_record_presence="logical_and_physical",
            artifact_physical_presence="verified",
            artifact_sha256=observed,
        )
    if verification_checks is not None:
        checks_audit, checks_reason = _bounded_verification(
            verification_checks, collected_rows, artifact_ids, board=board,
        )
        if checks_audit is None:
            return None, checks_reason
        audit.update(checks_audit)
    return audit, ""


def _stable_digest(value: Any, limit: int = 100) -> str:
    """Return a bounded, redaction-safe identity for worker-controlled text."""
    text = _redact(value, 10_000)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:limit]


def _ids(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _json_marker(kind: str, payload: Mapping[str, Any]) -> str:
    from hermes_cli import kanban_db

    body = json.dumps(
        {"kind": kind, **dict(payload)}, ensure_ascii=False, sort_keys=True, default=str,
    )
    return f"[night-operator:{kind}]\n{kanban_db.redact_review_value(body)}"


def _board_slug(value: Any) -> str:
    """Normalize the single board identity shared by hooks, sweep, and CLI."""
    try:
        from hermes_cli.kanban_db import _normalize_board_slug
    except ImportError:
        return "default"
    normalized = _normalize_board_slug(_text(value, 100))
    return normalized or "default"


def review_key(board: str, batch: str, expected_ids: Sequence[str]) -> str:
    board_name = _board_slug(board)
    if not _BOARD_RE.fullmatch(board_name):
        raise ValueError("board must be a simple slug")
    identity = {
        "board": board_name,
        "batch": _key_component(batch, 500),
        "expected_ids": sorted(_key_component(item, 200) for item in _ids(expected_ids)),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"operator-review:{board_name}:v2:{digest}"


def _decision_key(board: str, task_id: str) -> str:
    return f"operator-decision:{_key_component(board, 100)}:{_key_component(task_id, 200)}"


def _remediation_key(board: str, source_id: str, verification_id: str) -> str:
    return (
        f"operator-remediation:{_key_component(board, 100)}:"
        f"{_key_component(source_id, 200)}:{_key_component(verification_id, 200)}"
    )


def _escalation_key(board: str, source_id: str, verification_id: str) -> str:
    return (
        f"operator-escalation:{_key_component(board, 100)}:"
        f"{_key_component(source_id, 200)}:{_key_component(verification_id, 200)}"
    )


def _is_internal_task(task: Any) -> bool:
    """Recognize plugin-owned cards by reserved key plus a machine marker.

    This is a structural guard, not cryptographic authentication: a board writer
    able to forge both fields can still spoof a card. Titles are deliberately not
    sufficient because worker-controlled titles are untrusted input.
    """
    key = str(getattr(task, "idempotency_key", "") or "")
    body = str(getattr(task, "body", "") or "")
    return key.startswith(_OPERATOR_PREFIXES) and "[night-operator:" in body


def _get_task(conn: Any, task_id: str):
    from hermes_cli import kanban_db

    return kanban_db.get_task(conn, task_id)


def get_task(conn: Any, task_id: str):
    """Read a task through the native Kanban API."""
    return _get_task(conn, task_id)


def _latest_run(conn: Any, task_id: str):
    from hermes_cli import kanban_db

    return kanban_db.latest_run(conn, task_id)


def _handoff(conn: Any, task_id: str) -> dict[str, Any]:
    """Read task, structured run metadata, comments and events."""
    from hermes_cli import kanban_db

    task = kanban_db.get_task(conn, task_id)
    run = kanban_db.latest_run(conn, task_id) if task is not None else None
    metadata = dict(run.metadata) if run is not None and isinstance(run.metadata, Mapping) else {}
    return {
        "task": task,
        "run": run,
        "metadata": metadata,
        "summary": _text(getattr(run, "summary", "")),
        "comments": list(kanban_db.list_comments(conn, task_id)) if task is not None else [],
        "events": list(kanban_db.list_events(conn, task_id)) if task is not None else [],
    }


def _batch_from_metadata(metadata: Mapping[str, Any], review_group_key: str) -> tuple[str | None, list[str]]:
    batch = metadata.get(review_group_key) or metadata.get("review_group") or metadata.get("batch_id")
    expected = metadata.get("operator_expected_ids")
    if expected is None:
        expected = metadata.get("expected_ids")
    return (_text(batch) or None), _ids(expected)


def _ensure_comment(conn: Any, task_id: str, marker: str) -> None:
    from hermes_cli import kanban_db

    if any(marker in comment.body for comment in kanban_db.list_comments(conn, task_id)):
        return
    kanban_db.add_comment(conn, task_id, "night-operator", marker)


def _create_native(conn: Any, **kwargs: Any) -> str:
    """Create one task while serializing the idempotency check and insert."""
    from hermes_cli import kanban_db

    key = kwargs.get("idempotency_key")
    if not key:
        raise ValueError("idempotency_key is required")
    with kanban_db.write_txn(conn):
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is not None:
            return str(row["id"])
        return kanban_db.create_task(conn, **kwargs)


def _create_via_adapter(conn: Any, adapter: Any, **kwargs: Any) -> str:
    if adapter is not None and hasattr(adapter, "create"):
        return str(adapter.create(**kwargs))
    if conn is None:
        raise RuntimeError("a native Kanban connection is required for a write")
    return _create_native(conn, **kwargs)


def _verification_body(board: str, batch: str, parent_ids: Sequence[str]) -> str:
    return _json_marker(
        "review",
        {
            "board": board,
            "batch_id": batch,
            "verified_parent_ids": list(parent_ids),
            "required_action": "Read every parent handoff, verify evidence, then choose one terminal outcome.",
        },
    )


def create_or_get_verification(
    conn: Any,
    *,
    board: str = "default",
    batch: str,
    expected_ids: Sequence[str],
    home: Path | None = None,
    adapter: Any = None,
    operator_profile: str = "night-operator",
) -> str:
    """Create/get exactly one verification card for a fully identified batch."""
    parent_ids = _ids(expected_ids)
    if not batch or not parent_ids:
        raise ValueError("batch and expected_ids are required")
    board_name = _board_slug(board)
    operator = _durable_text(operator_profile, 100) or "night-operator"
    safe_batch = _durable_text(batch, 500)
    key = review_key(board_name, batch, parent_ids)
    kwargs = {
        "title": f"Night review: {safe_batch}",
        "body": _verification_body(board_name, safe_batch, parent_ids),
        "assignee": operator,
        "created_by": operator,
        "parents": tuple(parent_ids),
        "initial_status": "running",
        "workspace_kind": "scratch",
        "board": board_name,
        "idempotency_key": key,
    }
    task_id = _create_via_adapter(conn, adapter, **kwargs)
    if conn is not None and adapter is None:
        _ensure_comment(conn, task_id, "[night-operator:review]")
    return task_id


def _decision_body(board: str, task_id: str, reason: str) -> str:
    return _json_marker(
        "decision",
        {"board": board, "source_task_id": task_id, "reason": reason, "human_only": True},
    )


def _create_decision(
    conn: Any, adapter: Any, *, board: str, task_id: str, reason: str,
    operator_profile: str = "night-operator",
) -> str | None:
    board_name = _board_slug(board)
    task_key = _durable_text(task_id, 200)
    safe_reason = _durable_text(reason, 2000)
    operator = _durable_text(operator_profile, 100) or "night-operator"
    kwargs = {
        "title": f"Operator decision required: {task_key}",
        "body": _decision_body(board_name, task_key, safe_reason),
        "assignee": operator,
        "created_by": operator,
        "initial_status": "blocked",
        "workspace_kind": "scratch",
        "board": board_name,
        "idempotency_key": _decision_key(board_name, task_key),
    }
    try:
        new_id = _create_via_adapter(conn, adapter, **kwargs)
    except Exception as exc:
        logger.warning("night operator decision card failed: %s", exc)
        return None
    if conn is not None and adapter is None:
        _ensure_comment(conn, new_id, "[night-operator:decision]")
    return new_id


def _batch_state(conn: Any, *, task_id: str, batch: str, expected_ids: Sequence[str]) -> str:
    """Return ready, waiting_for_batch, decision_required, or disabled."""
    parent_ids = _ids(expected_ids)
    if task_id not in parent_ids:
        return "decision_required"
    if conn is None:
        # No state source means fail closed. A caller with an explicit one-item
        # batch may proceed; a multi-card batch must be reconciled by the DB.
        return "ready" if len(parent_ids) == 1 else "waiting_for_batch"
    from hermes_cli import kanban_db

    source = kanban_db.get_task(conn, task_id)
    if source is None or source.status != "done":
        return "waiting_for_batch"
    for parent_id in parent_ids:
        parent = kanban_db.get_task(conn, parent_id)
        if parent is None:
            return "decision_required"
        if parent.status != "done":
            return "waiting_for_batch"
        run = kanban_db.latest_run(conn, parent_id)
        metadata = run.metadata if run is not None and isinstance(run.metadata, Mapping) else {}
        if metadata.get("operator_group_ready") is False:
            return "waiting_for_batch"
    return "ready"


def _handle_completed_conn(
    conn: Any,
    *,
    task_id: str,
    board: str,
    batch: str | None,
    expected_ids: Sequence[str] | None,
    adapter: Any,
    config: Config,
) -> Outcome:
    parent_ids = _ids(expected_ids)
    if not batch or not parent_ids:
        reason = "completed task has no unambiguous operator_batch and expected_ids"
        if config.dry_run:
            return Outcome(
                "decision_required", requires_human=True, reason=reason,
                metadata={"decision_task_id": None},
            )
        decision_id = _create_decision(
            conn, adapter, board=board, task_id=task_id, reason=reason,
            operator_profile=config.operator_profile,
        )
        return Outcome(
            "decision_required", requires_human=True, reason=reason,
            metadata={"decision_task_id": decision_id},
        )
    state = _batch_state(conn, task_id=task_id, batch=batch, expected_ids=parent_ids)
    if state == "waiting_for_batch":
        return Outcome("waiting_for_batch", reason="batch is incomplete or a parent is not done")
    if state == "decision_required":
        reason = "completed task is not proven to belong to the requested batch"
        if config.dry_run:
            return Outcome(
                "decision_required", requires_human=True, reason=reason,
                metadata={"decision_task_id": None},
            )
        decision_id = _create_decision(
            conn, adapter, board=board, task_id=task_id, reason=reason,
            operator_profile=config.operator_profile,
        )
        return Outcome(
            "decision_required", requires_human=True, reason=reason,
            metadata={"decision_task_id": decision_id},
        )
    if config.dry_run:
        return Outcome("dry_run", metadata={"batch_id": batch, "parent_ids": parent_ids})
    verification_id = create_or_get_verification(
        conn, board=board, batch=batch, expected_ids=parent_ids, adapter=adapter,
        operator_profile=config.operator_profile,
    )
    return Outcome(
        "verification_created",
        metadata={"verification_task_id": verification_id, "batch_id": batch, "parent_ids": parent_ids},
    )


def handle_completed(
    *,
    task_id: str,
    board: str = "default",
    batch: str | None = None,
    expected_ids: Sequence[str] | None = None,
    adapter: Any = None,
    home: Path | None = None,
    conn: Any = None,
    config: Config | None = None,
) -> Outcome:
    """Handle one native completed event without running an LLM in the hook."""
    cfg = config or Config()
    if not cfg.enabled:
        return Outcome("disabled")
    if conn is not None:
        return _handle_completed_conn(
            conn, task_id=task_id, board=board, batch=batch,
            expected_ids=expected_ids or (), adapter=adapter, config=cfg,
        )
    if home is not None:
        with _open_board(home, board) as opened:
            return _handle_completed_conn(
                opened, task_id=task_id, board=board, batch=batch,
                expected_ids=expected_ids or (), adapter=adapter, config=cfg,
            )
    return _handle_completed_conn(
        None, task_id=task_id, board=board, batch=batch,
        expected_ids=expected_ids or (), adapter=adapter, config=cfg,
    )


def decide(
    card: ReviewEvidence,
    decision: str,
    *,
    implementation_profile: str,
    evidence: Sequence[str],
    artifacts: Sequence[str] = (),
    next_steps: Sequence[str] = (),
    question: str = "",
    options: Sequence[str] = (),
) -> Outcome:
    """Validate one and only one terminal review decision."""
    operator = _text(card.operator_profile).lower()
    implementation = _text(implementation_profile).lower()
    if not operator or not implementation:
        raise ValueError("operator and implementation profiles are required")
    if operator == implementation or operator in {str(x).lower() for x in card.implementation_profiles}:
        raise ValueError("operator cannot self-approve its own implementation")
    round_no = int(card.review_round or 1)
    if round_no > int(card.max_review_rounds or DEFAULTS["max_review_rounds"]):
        return Outcome(
            "exhausted", requires_human=True,
            reason=f"review round limit {card.max_review_rounds} reached",
        )
    choice = str(decision or "").strip().lower()
    if choice == "pass":
        evidence_ids = _ids(evidence)
        artifact_ids = _safe_exact_artifact_identifiers(artifacts)
        parent_ids = _native_parent_claims(card.verified_parent_ids)
        if parent_ids is None:
            raise ValueError("pass requires exact verified parents")
        if not evidence_ids or artifact_ids is None:
            raise ValueError("pass requires evidence and redaction-invariant exact artifacts")
        return Outcome(
            "complete",
            metadata=_redact_structure({
                "verified_parent_ids": parent_ids,
                "verification_evidence": evidence_ids,
                "artifacts": artifact_ids,
                "residual_risk": list(card.residual_risk),
            }),
        )
    if choice in {"changes", "follow_up"}:
        steps = _ids(next_steps)
        if not steps:
            raise ValueError(f"{choice} requires concrete next steps")
        if round_no >= int(card.max_review_rounds or DEFAULTS["max_review_rounds"]):
            return Outcome(
                "exhausted", requires_human=True,
                reason=f"review round limit {card.max_review_rounds} reached",
                metadata={"review_round": round_no},
            )
        return Outcome(choice, reason="\n".join(f"{i + 1}. {step}" for i, step in enumerate(steps)))
    if choice in {"needs_input", "blocked"}:
        if not _text(question):
            raise ValueError("blocked decision requires a human question")
        return Outcome(
            "blocked", requires_human=True, reason=_text(question), question=_text(question),
            options=tuple(str(item) for item in options),
        )
    raise ValueError(f"unknown review decision: {decision!r}")


def _remediation_body(source_id: str, handoff: str, reason: str) -> str:
    return _json_marker(
        "remediation",
        {"source_parent_id": source_id, "next_steps": _text(reason), "handoff": _text(handoff, 2000)},
    )


def _create_followup(
    conn: Any,
    adapter: Any,
    *,
    board: str,
    source_id: str,
    verification_id: str,
    implementation_profile: str,
    reason: str,
    handoff: str,
    escalation: bool = False,
) -> str:
    board_name = _board_slug(board)
    source_key = _durable_text(source_id, 200)
    implementation = _durable_text(implementation_profile, 100) or "night-operator"
    key = (_escalation_key if escalation else _remediation_key)(board_name, source_key, verification_id)
    prefix = "Escalation" if escalation else "Remediation"
    body = _json_marker(
        "escalation" if escalation else "remediation",
        {
            "source_parent_id": source_id,
            "verification_task_id": verification_id,
            "next_steps": _durable_text(reason, 2000),
            "handoff": _durable_text(handoff, 2000),
        },
    )
    kwargs = {
        "title": f"{prefix}: {source_key}",
        "body": body,
        "assignee": "night-operator" if escalation else implementation,
        "created_by": "night-operator",
        "parents": (source_key,),
        "initial_status": "blocked" if escalation else "running",
        "workspace_kind": "scratch",
        "board": board_name,
        "idempotency_key": key,
    }
    task_id = _create_via_adapter(conn, adapter, **kwargs)
    if conn is not None and adapter is None:
        _ensure_comment(conn, task_id, f"[night-operator:{prefix.lower()}]")
    return task_id


def apply_outcome(
    conn: Any,
    *,
    verification_task_id: str,
    source_status: str,
    decision: str,
    reason: str = "",
    evidence: Sequence[str] = (),
    artifacts: Sequence[str] = (),
    verified_parent_ids: Sequence[str] = (),
    residual_risk: Sequence[str] = (),
    implementation_profile: str = "night-operator",
    source_parent_id: str | None = None,
    source_handoff: str = "",
    board: str = "default",
    adapter: Any = None,
    max_review_rounds: int = 2,
    physical_sha256: Mapping[str, str] | None = None,
    verification_checks: Sequence[str] | None = None,
) -> Outcome:
    """Apply one validated decision through native same-card or follow-up APIs."""
    from hermes_cli import kanban_db

    choice = str(decision or "").strip().lower()
    if source_status == "review":
        native_implementation = _implementation_profile_for(
            conn, verification_task_id=verification_task_id,
            source_parent_id=None, source_status="review",
        )
        if not native_implementation:
            return Outcome(
                "blocked", requires_human=True,
                reason="review handoff has no native implementer run",
            )
        if native_implementation != _profile_key(implementation_profile):
            return Outcome(
                "blocked", requires_human=True,
                reason="implementation profile does not match native run history",
            )
        operator = native_implementation
        # The claimed run, not a literal profile name, is the authoritative
        # operator identity.  This keeps self-approval fail-closed when the
        # profile is configured under a custom name.
        active_profile = _active_run_profile(conn, verification_task_id)
        if not active_profile:
            return Outcome(
                "blocked", requires_human=True,
                reason="review card has no active reviewer run",
            )
        if active_profile == operator:
            return Outcome(
                "blocked", requires_human=True,
                reason="operator cannot approve its own implementation",
            )
        task = kanban_db.get_task(conn, verification_task_id)
        if task is None:
            return Outcome("blocked", requires_human=True, reason="verification task not found")
        if choice == "changes":
            round_limit = _durable_int(max_review_rounds, DEFAULTS["max_review_rounds"])
            completed_rounds = sum(
                1 for event in kanban_db.list_events(conn, verification_task_id)
                if getattr(event, "kind", "") == "changes_requested"
            )
            if completed_rounds >= round_limit:
                escalation_source = _durable_text(source_parent_id or verification_task_id, 200)
                escalation_id = _create_followup(
                    conn, adapter, board=board, source_id=escalation_source,
                    verification_id=verification_task_id,
                    implementation_profile=implementation_profile,
                    reason=reason or f"review round limit {round_limit} reached",
                    handoff=source_handoff, escalation=True,
                )
                return Outcome(
                    "exhausted", requires_human=True,
                    reason=f"review round limit {round_limit} reached",
                    metadata={"escalation_task_id": escalation_id, "review_round": completed_rounds},
                )
            if task.status == "review":
                claimed = kanban_db.claim_review_task(conn, verification_task_id, claimer="night-operator")
                task = claimed or task
            if task.status != "running" or task.current_run_id is None:
                return Outcome("blocked", requires_human=True, reason="review card has no active reviewer run")
            ok, detail = kanban_db.request_changes(
                conn, verification_task_id, reason=reason, expected_run_id=task.current_run_id,
            )
            if not ok:
                return Outcome("blocked", requires_human=True, reason=reason or str(detail))
            _ensure_comment(conn, verification_task_id, "[night-operator:changes]")
            return Outcome("changes", reason=reason)
        if choice == "pass":
            evidence_ids = _safe_ids(evidence)
            artifact_ids = _safe_exact_artifact_identifiers(artifacts)
            if not evidence_ids or artifact_ids is None:
                return Outcome("blocked", requires_human=True, reason="pass requires evidence and redaction-invariant exact artifacts")
            native_parent_ids, parent_reason = _native_parent_audit(
                conn, verification_task_id, verified_parent_ids,
            )
            if native_parent_ids is None:
                return Outcome("blocked", requires_human=True, reason=parent_reason)
            artifact_audit, artifact_reason = _verify_attachment_records(
                conn, verification_task_id=verification_task_id, artifacts=artifacts,
                physical_sha256=physical_sha256, verification_checks=verification_checks,
                board=_board_slug(board),
            )
            if artifact_audit is None:
                return Outcome("blocked", requires_human=True, reason=artifact_reason)
            safe_reason = _redact(reason, 2000)
            metadata = {
                "night_operator_verified": True,
                "verified_parent_ids": native_parent_ids,
                "verification_evidence": evidence_ids,
                "artifacts": artifact_ids,
                "residual_risk": _safe_ids(residual_risk),
                **artifact_audit,
            }
            expected_run = task.current_run_id
            ok = kanban_db.complete_task(
                conn, verification_task_id,
                summary=f"Night review passed: {safe_reason}".strip(),
                metadata=metadata,
                expected_run_id=expected_run,
            )
            return Outcome("complete" if ok else "blocked", reason=reason, metadata=metadata)
        if choice in {"needs_input", "blocked"}:
            if task.status == "review":
                task = kanban_db.claim_review_task(conn, verification_task_id, claimer="night-operator") or task
            expected_run = task.current_run_id
            if task.status != "running" or expected_run is None:
                return Outcome("blocked", requires_human=True, reason="review card has no active reviewer run")
            ok = kanban_db.block_task(
                conn, verification_task_id, reason=reason or "human decision required",
                kind="needs_input", expected_run_id=expected_run,
            )
            if ok:
                _ensure_comment(conn, verification_task_id, "[night-operator:blocked]")
                return Outcome("blocked", requires_human=True, reason=reason)
            return Outcome("blocked", requires_human=True, reason=reason or "block transition refused")
        return Outcome("blocked", requires_human=True, reason=f"unsupported decision: {decision!r}")

    if source_status == "done":
        if not source_parent_id:
            return Outcome("blocked", requires_human=True, reason="done source has no parent id")
        source = kanban_db.get_task(conn, source_parent_id)
        if source is None or source.status != "done":
            return Outcome("blocked", requires_human=True, reason="done source is not immutable done")
        native_parent_ids, parent_bound_reason = _bounded_native_parent_ids(
            conn, verification_task_id,
        )
        if native_parent_ids is None:
            return Outcome("blocked", requires_human=True, reason=parent_bound_reason)
        if _durable_text(source_parent_id, 200) not in native_parent_ids:
            return Outcome(
                "blocked", requires_human=True,
                reason="done source is not a native parent of the verification card",
            )
        native_implementation = _implementation_profile_for(
            conn, verification_task_id=verification_task_id,
            source_parent_id=source_parent_id, source_status="done",
        )
        if not native_implementation:
            return Outcome(
                "blocked", requires_human=True,
                reason="done source has no native implementer run",
            )
        if native_implementation != _profile_key(implementation_profile):
            return Outcome(
                "blocked", requires_human=True,
                reason="implementation profile does not match native run history",
            )
        if choice in {"changes", "follow_up"}:
            followup = _create_followup(
                conn, adapter, board=board, source_id=source_parent_id,
                verification_id=verification_task_id, implementation_profile=implementation_profile,
                reason=reason, handoff=source_handoff,
            )
            terminal_state = "follow_up" if choice == "follow_up" else "remediation"
            return Outcome(terminal_state, metadata={"followup_task_id": followup})
        if choice in {"needs_input", "blocked"}:
            if not _durable_text(reason, 2000):
                return Outcome("blocked", requires_human=True, reason="human decision required")
            followup = _create_followup(
                conn, adapter, board=board, source_id=source_parent_id,
                verification_id=verification_task_id, implementation_profile=implementation_profile,
                reason=reason, handoff=source_handoff, escalation=True,
            )
            return Outcome("blocked", requires_human=True, metadata={"escalation_task_id": followup})
        if choice == "pass":
            evidence_ids = _safe_ids(evidence)
            artifact_ids = _safe_exact_artifact_identifiers(artifacts)
            if not evidence_ids or artifact_ids is None:
                return Outcome("blocked", requires_human=True, reason="pass has no redaction-invariant exact artifact evidence")
            task = kanban_db.get_task(conn, verification_task_id)
            if task is None:
                return Outcome("blocked", requires_human=True, reason="verification task not found")
            if task.status != "running" or task.current_run_id is None:
                return Outcome(
                    "blocked", requires_human=True,
                    reason="verification card has no active operator run",
                )
            if not _is_internal_task(task):
                return Outcome(
                    "blocked", requires_human=True,
                    reason="verification card is not a plugin-owned internal card",
                )
            expected_operator = _profile_key(getattr(task, "created_by", ""))
            active_operator = _active_run_profile(conn, verification_task_id)
            if not expected_operator or not active_operator:
                return Outcome(
                    "blocked", requires_human=True,
                    reason="verification card has no verifiable operator identity",
                )
            if active_operator != expected_operator:
                return Outcome(
                    "blocked", requires_human=True,
                    reason="verification card is owned by a different operator profile",
                )
            if active_operator == _profile_key(implementation_profile):
                return Outcome(
                    "blocked", requires_human=True,
                    reason="operator cannot approve its own implementation",
                )
            audited_parent_ids, parent_reason = _native_parent_audit(
                conn, verification_task_id, verified_parent_ids,
            )
            if audited_parent_ids is None:
                return Outcome("blocked", requires_human=True, reason=parent_reason)
            artifact_audit, artifact_reason = _verify_attachment_records(
                conn, verification_task_id=verification_task_id, artifacts=artifacts,
                source_parent_id=source_parent_id,
                physical_sha256=physical_sha256, verification_checks=verification_checks,
                board=_board_slug(board),
            )
            if artifact_audit is None:
                return Outcome("blocked", requires_human=True, reason=artifact_reason)
            metadata = {
                "night_operator_verified": True,
                "verified_parent_ids": audited_parent_ids,
                "verification_evidence": evidence_ids,
                "artifacts": artifact_ids,
                "residual_risk": _safe_ids(residual_risk),
                **artifact_audit,
            }
            ok = kanban_db.complete_task(
                conn, verification_task_id, summary=f"Night review passed: {_redact(reason, 2000)}",
                metadata=metadata, expected_run_id=task.current_run_id,
            )
            return Outcome("complete" if ok else "blocked", reason=_redact(reason, 2000), metadata=metadata)
    return Outcome("blocked", requires_human=True, reason=f"unsupported source status: {source_status!r}")


def _external_candidates(conn: Any, *, status: str, limit: int) -> list[Any]:
    """Return at most ``limit`` non-internal tasks in native completion order.

    Filtering in SQL is required for a hard bound: fetching ``limit`` rows and
    discarding plugin-owned cards would let internal history starve external
    work forever, while scanning an unbounded backlog would defeat the bound.
    """
    prefixes = tuple(_OPERATOR_PREFIXES)
    key_clauses = " OR ".join(
        "substr(COALESCE(idempotency_key, ''), 1, ?) = ?" for _ in prefixes
    )
    params: list[Any] = [status]
    for prefix in prefixes:
        params.extend((len(prefix), prefix))
    params.append(limit)
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = ? AND status != 'archived' "
        f"AND NOT (({key_clauses}) AND instr(COALESCE(body, ''), '[night-operator:') > 0) "
        "ORDER BY completed_at DESC NULLS LAST, id DESC LIMIT ?",
        params,
    ).fetchall()
    from hermes_cli import kanban_db

    return [kanban_db.Task.from_row(row) for row in rows]


def _blocked_escalation_reason(task: Any) -> str:
    kind = _text(getattr(task, "block_kind", ""), 80)
    detail = f" ({kind})" if kind else ""
    return f"Kanban task is {task.status}{detail}; operator decision is required"


def reconcile(conn: Any, *, config: Config, home: Path | None = None) -> list[Outcome]:
    """Run one bounded, idempotent sweep over blocked and completed tasks.

    Blocked cards are included here, not written by the lifecycle hook. This is
    the restart-safe path: the hook only records a post-transaction request, and
    a later sweep can discover the card even if the process restarted.
    """
    if not config.enabled:
        return []
    from hermes_cli import kanban_db

    limit = max(0, min(int(config.max_items), 1000))
    if limit == 0:
        return []
    board_name = _board_slug(config.board)
    config = Config(**{**config.__dict__, "board": board_name})
    candidates: list[Any] = []
    # Completed implementation handoffs are the operator's primary input. Keep
    # blocked/triage from consuming the whole bounded sweep before done is seen.
    for status in ("done", "blocked", "triage"):
        remaining = limit - len(candidates)
        if remaining <= 0:
            break
        candidates.extend(_external_candidates(conn, status=status, limit=remaining))
    results: list[Outcome] = []
    seen: set[str] = set()
    for task in candidates:
        if _is_internal_task(task):
            continue
        if task.status in {"blocked", "triage"}:
            reason = _blocked_escalation_reason(task)
            decision_id = None
            if not config.dry_run:
                decision_id = _create_decision(
                    conn, None, board=board_name, task_id=task.id, reason=reason,
                    operator_profile=config.operator_profile,
                )
            results.append(Outcome(
                "decision_required", requires_human=True, reason=reason,
                metadata={"decision_task_id": decision_id, "source_task_id": task.id},
            ))
            continue
        run = kanban_db.latest_run(conn, task.id)
        metadata = run.metadata if run is not None and isinstance(run.metadata, Mapping) else {}
        batch, expected = _batch_from_metadata(metadata, config.review_group_key)
        if not batch or not expected:
            if config.dry_run:
                continue
            reason = "completed task has no unambiguous operator_batch and expected_ids"
            decision_id = _create_decision(
                conn, None, board=board_name, task_id=task.id, reason=reason,
                operator_profile=config.operator_profile,
            )
            results.append(Outcome(
                "decision_required", requires_human=True, reason=reason,
                metadata={"decision_task_id": decision_id, "source_task_id": task.id},
            ))
            continue
        key = review_key(board_name, batch, expected)
        if key in seen:
            continue
        seen.add(key)
        if config.dry_run:
            state = _batch_state(conn, task_id=task.id, batch=batch, expected_ids=expected)
            results.append(Outcome(
                "dry_run" if state == "ready" else state,
                metadata={"batch_id": batch, "parent_ids": expected},
            ))
            continue
        results.append(_handle_completed_conn(
            conn, task_id=task.id, board=board_name, batch=batch,
            expected_ids=expected, adapter=None, config=config,
        ))
    return results


def render_notice(board: str, card: ReviewEvidence, outcome: Outcome) -> dict[str, Any]:
    """Return a minimal, secret-free escalation payload for a human channel."""
    return {
        "board": _redact(board, 100),
        "task_id": _redact(card.verification_task_id, 200),
        "batch_id": _redact(card.batch_id or "unknown", 500),
        "summary": _redact(outcome.reason or "Night Operator requires a decision", 500),
        "reason": _redact(outcome.reason, 500),
        "question": _redact(outcome.question, 500),
        "options": [_redact(item, 500) for item in outcome.options],
        "link": f"kanban://{_key_component(board, 100)}/tasks/{_key_component(card.verification_task_id, 200)}",
    }


_CHANNEL_RE = re.compile(r"^([a-z][a-z0-9_]{1,31}):([A-Za-z0-9_.:-]{1,128})$")


def _parse_channel(value: Any) -> tuple[str, str] | None:
    """Parse ``platform:chat_id`` strictly; anything else is not a channel."""
    if not isinstance(value, str):
        return None
    match = _CHANNEL_RE.match(value.strip())
    if match is None:
        return None
    platform, chat_id = match.group(1), match.group(2)
    if platform in _DENIED_TOOLS or platform.startswith("hermes_"):
        return None
    return platform, chat_id


def deliver_notice(
    conn: Any, notice: Mapping[str, Any], *, task_id: str, channel: Any,
) -> dict[str, Any]:
    """Register a native terminal-state subscription for a human channel.

    Delivery is owned by the native notifier; this only subscribes the task, so
    a configured channel is a real notification path and an absent or malformed
    channel fails closed instead of silently claiming an escalation was sent.
    """
    parsed = _parse_channel(channel)
    if parsed is None:
        return {"ok": False, "reason": "no_channel"}
    platform, chat_id = parsed
    from hermes_cli import kanban_db_notify

    try:
        kanban_db_notify.add_notify_sub(
            conn, task_id=task_id, platform=platform, chat_id=chat_id,
            notifier_profile=_text(DEFAULTS["operator_profile"], 100),
        )
    except Exception:
        return {"ok": False, "reason": "subscribe_failed"}
    return {"ok": True, "reason": "subscribed", "platform": platform, "chat_id": chat_id}


def dispatch_outcome_notice(
    conn: Any, board: str, card: ReviewEvidence, outcome: Outcome, *,
    escalation_channel: Any = None,
) -> dict[str, Any]:
    """Escalate only what a human must decide; a passed review stays silent."""
    if not outcome.requires_human:
        return {"ok": True, "reason": "not_required"}
    notice = render_notice(board, card, outcome)
    result = deliver_notice(
        conn, notice, task_id=card.verification_task_id, channel=escalation_channel,
    )
    return {**result, "notice": notice}


def tool_policy() -> dict[str, Any]:
    return {
        "allowed": ["kanban_read", "kanban_review", "kanban_followup"],
        "denied": ["terminal", "browser", "credentials", "deployment", "arbitrary_external_communication"],
        "approvals": {
            "cron_mode": "deny",
            "single_query_mode": "deny",
            "unattended_mode": "deny",
        },
    }


# --- Native board path and hook wiring -------------------------------------

def _board_path(home: Path | None, board: str) -> Path:
    board_name = _board_slug(board)
    if not _BOARD_RE.fullmatch(board_name):
        raise ValueError("board must be a simple slug")
    if home is not None:
        root = Path(home)
        return root / "kanban.db" if board_name == "default" else root / "kanban" / "boards" / board_name / "kanban.db"
    from hermes_cli import kanban_db

    return kanban_db.kanban_db_path(board=board_name)


@contextmanager
def _open_board(home: Path | None, board: str) -> Iterator[Any]:
    from hermes_cli import kanban_db_connect as kbc

    path = _board_path(home, board)
    if not path.exists():
        raise FileNotFoundError(f"Kanban board does not exist: {path}")
    with kbc.connect_closing(db_path=path) as conn:
        yield conn


def _config_from_context(ctx: Any) -> Config:
    def read(name: str, default: Any) -> Any:
        try:
            return ctx.get_config(name, default)
        except Exception:
            return default

    return Config(
        enabled=_strict_bool(read("enabled", DEFAULTS["enabled"]), DEFAULTS["enabled"]),
        dry_run=_strict_bool(read("dry_run", DEFAULTS["dry_run"]), DEFAULTS["dry_run"]),
        board=_text(read("board", DEFAULTS["board"]), 100) or "default",
        review_group_key=_text(read("review_group_key", DEFAULTS["review_group_key"]), 100) or "operator_batch",
        max_review_rounds=_durable_int(read("max_review_rounds", DEFAULTS["max_review_rounds"]), DEFAULTS["max_review_rounds"]),
        reconciliation_interval_seconds=_durable_int(read("reconciliation_interval_seconds", DEFAULTS["reconciliation_interval_seconds"]), DEFAULTS["reconciliation_interval_seconds"]),
        escalation_channel=read("escalation_channel", DEFAULTS["escalation_channel"]),
        max_items=_durable_int(read("max_items", DEFAULTS["max_items"]), DEFAULTS["max_items"]),
        operator_profile=_text(read("operator_profile", "night-operator"), 100) or "night-operator",
        enforce_tools=_strict_bool(read("enforce_tools", DEFAULTS["enforce_tools"]), DEFAULTS["enforce_tools"]),
    )


def _plan_reconciliation(board: str, task_id: str, delay: int) -> None:
    """Record a post-transaction sweep request; the hook itself never writes."""
    key = f"{_text(board, 100) or 'default'}\0{_text(task_id, 200)}"
    with _PLANNED_RECONCILIATION_LOCK:
        _PLANNED_RECONCILIATION_TASKS[key] = time.monotonic() + max(0, int(delay))


def _consume_planned_reconciliation(board: str, task_id: str) -> bool:
    """Claim a queued request once; stale entries are never replayed."""
    key = f"{_text(board, 100) or 'default'}\0{_text(task_id, 200)}"
    now = time.monotonic()
    with _PLANNED_RECONCILIATION_LOCK:
        deadline = _PLANNED_RECONCILIATION_TASKS.pop(key, None)
        if deadline is None:
            return False
        if deadline > now:
            _PLANNED_RECONCILIATION_TASKS[key] = deadline
            return False
        return True


def _hook_task_is_internal(conn: Any, task_id: str) -> bool:
    task = _get_task(conn, task_id)
    return task is not None and _is_internal_task(task)


def _on_dispatch_tick(ctx: Any, *, board: str | None = None, dry_run: bool = False, **_: Any) -> None:
    """Post-lock dispatcher observer; performs the deferred, bounded sweep."""
    if dry_run is True:
        return
    config = _config_from_context(ctx)
    if not config.enabled or config.dry_run:
        return
    board_name = _board_slug(board or config.board)
    state = getattr(ctx, "state", None)
    if state is not None:
        try:
            now_unix = float(time.time())
            last_unix = float(state.get("reconcile:last_unix", 0.0))
            if 0.0 < last_unix <= now_unix and now_unix - last_unix < config.reconciliation_interval_seconds:
                return
            state.set("reconcile:last_unix", now_unix)
        except Exception as exc:
            logger.warning("night operator reconciliation state unavailable: %s", exc)
            return
    else:
        now_monotonic = time.monotonic()
        with _PLANNED_RECONCILIATION_LOCK:
            if now_monotonic - _PLANNED_RECONCILIATION_LAST_RUN.get(board_name, 0.0) < config.reconciliation_interval_seconds:
                return
            _PLANNED_RECONCILIATION_LAST_RUN[board_name] = now_monotonic
    config = Config(**{**config.__dict__, "board": board_name})
    try:
        with _open_board(None, board_name) as conn:
            if not config.dry_run:
                now = time.monotonic()
                with _PLANNED_RECONCILIATION_LOCK:
                    planned = [
                        key for key, deadline in list(_PLANNED_RECONCILIATION_TASKS.items())
                        if deadline <= now and key.partition("\0")[0] == board_name
                    ]
                for key in planned:
                    board_part, _, task_part = key.partition("\0")
                    _consume_planned_reconciliation(board_part, task_part)
            reconcile(conn, config=config)
    except Exception as exc:
        logger.warning("night operator reconciliation tick failed closed: %s", exc)


def _on_completed(ctx: Any, *, task_id: str, board: str = "default", **_: Any) -> None:
    config = _config_from_context(ctx)
    if not config.enabled or config.dry_run:
        return
    try:
        with _open_board(None, board) as conn:
            if _hook_task_is_internal(conn, task_id):
                return
            handoff = _handoff(conn, task_id)
            batch, expected = _batch_from_metadata(handoff["metadata"], config.review_group_key)
            handle_completed(
                task_id=task_id, board=board, batch=batch, expected_ids=expected,
                conn=conn, config=config,
            )
    except Exception as exc:
        logger.warning("night operator completed hook failed closed: %s", exc)


def _on_blocked(ctx: Any, *, task_id: str, board: str = "default", **_: Any) -> None:
    """Observe a blocked transition; never open a second writer under its transaction."""
    config = _config_from_context(ctx)
    if not config.enabled or config.dry_run:
        return
    try:
        board_name = _text(board, 100) or "default"
        _plan_reconciliation(board_name, task_id, 0)
        logger.debug("night operator queued post-commit reconciliation for %s", task_id)
    except Exception as exc:
        logger.warning("night operator blocked observation failed closed: %s", exc)


def _on_claimed(**_: Any) -> None:
    """Claimed is diagnostic only; it never dispatches review work."""
    return None


def register(ctx: Any) -> None:
    """Register observers and the bounded reconciliation CLI; no model tool is exposed."""
    plugin_id = getattr(ctx, "plugin_id", "night-operator")
    def cli_handler(args: argparse.Namespace) -> int:
        return night_operator_command(args, plugin_id=plugin_id)
    ctx.register_hook("on_kanban_dispatch_tick", lambda **kwargs: _on_dispatch_tick(ctx, **kwargs))
    ctx.register_hook("kanban_task_claimed", _on_claimed)
    ctx.register_hook("kanban_task_completed", lambda **kwargs: _on_completed(ctx, **kwargs))
    ctx.register_hook("kanban_task_blocked", lambda **kwargs: _on_blocked(ctx, **kwargs))
    ctx.register_hook("pre_tool_call", lambda **kwargs: _on_pre_tool_call(ctx, **kwargs))
    ctx.register_cli_command(
        name="night-operator",
        help="Inspect or run one bounded native Kanban reconciliation sweep",
        setup_fn=register_cli,
        handler_fn=cli_handler,
        description=(
            "Native Hermes Kanban night-operator control plane. The default sweep is read-only; "
            "writes require an explicit --write switch and the plugin's enabled + non-dry-run config."
        ),
    )


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes night-operator`` without adding a core CLI command."""
    subs = parser.add_subparsers(dest="night_operator_action")
    sweep = subs.add_parser("sweep", help="Run one bounded reconciliation sweep")
    sweep.add_argument("--board", default=DEFAULTS["board"], help="Board slug to inspect")
    sweep.add_argument(
        "--home", default=None,
        help="Optional Hermes home/board parent for an isolated dry-run or acceptance run",
    )
    sweep.add_argument(
        "--max-items", type=int, default=int(DEFAULTS["max_items"]),
        help="Maximum number of done candidates inspected in this sweep",
    )
    sweep.add_argument(
        "--write", action="store_true",
        help="Allow writes; still requires enabled=true and dry_run=false in plugin settings",
    )
    sweep.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(func=night_operator_command)


def _cli_config(home: Path | None = None, plugin_id: str = "night-operator") -> Config:
    """Read the effective plugin settings without assuming a flat install path.

    A bundled/plugin install normally has the flat id ``night-operator``; a
    category install has a path-derived id such as ``category/night_operator``.
    The caller may provide the id, and an unambiguous basename match is kept as
    a compatibility fallback. The CLI remains read-only with respect to config.
    """
    cfg = Config()
    if home is not None:
        config_path = home / "config.yaml"
    else:
        from hermes_constants import get_hermes_home
        config_path = Path(get_hermes_home()) / "config.yaml"
    try:
        import yaml
    except Exception:
        return cfg
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        entries = raw.get("plugins", {}).get("entries", {})
        entry = entries.get(plugin_id, {}) if isinstance(entries, Mapping) else {}
        if not isinstance(entry, Mapping):
            entry = {}
        if not entry and isinstance(entries, Mapping):
            matches = [
                value for key, value in entries.items()
                if str(key).rsplit("/", 1)[-1] == plugin_id.rsplit("/", 1)[-1]
                and isinstance(value, Mapping)
            ]
            if len(matches) == 1:
                entry = matches[0]
        settings = entry.get("settings", {}) if isinstance(entry, Mapping) else {}
        if not isinstance(settings, Mapping):
            return cfg
        return Config(
            enabled=_strict_bool(settings.get("enabled", cfg.enabled), cfg.enabled),
            dry_run=_strict_bool(settings.get("dry_run", cfg.dry_run), cfg.dry_run),
            board=_text(settings.get("board", cfg.board), 100) or cfg.board,
            review_group_key=_text(settings.get("review_group_key", cfg.review_group_key), 100) or cfg.review_group_key,
            max_review_rounds=_durable_int(settings.get("max_review_rounds", cfg.max_review_rounds), cfg.max_review_rounds),
            reconciliation_interval_seconds=_durable_int(settings.get(
                "reconciliation_interval_seconds", cfg.reconciliation_interval_seconds), cfg.reconciliation_interval_seconds),
            escalation_channel=settings.get("escalation_channel"),
            max_items=_durable_int(settings.get("max_items", cfg.max_items), cfg.max_items),
            operator_profile=_text(settings.get("operator_profile", cfg.operator_profile), 100) or cfg.operator_profile,
        )
    except (yaml.YAMLError, OSError, TypeError, ValueError, AttributeError):
        return cfg


def night_operator_command(args: argparse.Namespace, *, plugin_id: str = "night-operator") -> int:
    """CLI entry point for a bounded sweep; never dispatches a model turn."""
    if getattr(args, "night_operator_action", None) != "sweep":
        print("Usage: hermes night-operator sweep [--board BOARD] [--home HERMES_HOME] [--write] [--json]")
        return 2
    home_arg = getattr(args, "home", None)
    home = Path(home_arg) if home_arg else None
    config = _cli_config(home, plugin_id=plugin_id)
    if getattr(args, "max_items", None) is not None:
        config = Config(**{**config.__dict__, "max_items": _durable_int(args.max_items, config.max_items)})
    if getattr(args, "board", None):
        config = Config(**{**config.__dict__, "board": _text(args.board, 100) or config.board})
    # An explicit CLI --write is necessary but never sufficient: the persistent
    # kill switch and dry-run setting remain authoritative.
    if getattr(args, "write", False):
        if not config.enabled or config.dry_run:
            payload = {"ok": False, "reason": "write refused: enabled=true and dry_run=false are required"}
            print(json.dumps(payload, sort_keys=True) if getattr(args, "json", False) else payload["reason"])
            return 2
    else:
        config = Config(**{**config.__dict__, "dry_run": True})
    try:
        with _open_board(home, config.board) as conn:
            outcomes = reconcile(conn, config=config, home=home)
    except ValueError as exc:
        payload = {"ok": False, "reason": str(exc)}
        print(json.dumps(payload, sort_keys=True) if getattr(args, "json", False) else payload["reason"])
        return 2
    except Exception as exc:
        payload = {"ok": False, "reason": f"reconciliation unavailable: {exc}"}
        print(json.dumps(payload, sort_keys=True) if getattr(args, "json", False) else payload["reason"])
        return 1
    payload = {
        "ok": True,
        "board": config.board,
        "dry_run": config.dry_run,
        "count": len(outcomes),
        "outcomes": [
            {
                "state": item.terminal_state,
                "requires_human": item.requires_human,
                "reason": _redact(item.reason, 2000),
                "metadata": _redact_structure(item.metadata),
            }
            for item in outcomes
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0
