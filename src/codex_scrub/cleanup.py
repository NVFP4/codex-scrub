from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from glob import escape as glob_escape
from pathlib import Path

from codex_scrub.storage import (
    delete_path_candidate,
    goals_db_paths,
    is_filelike,
    logs_db_paths,
    memories_db_paths,
    quote_identifier,
    sqlite_columns,
    sqlite_home,
    state_db_paths,
    write_text_atomically,
)

STATE_DELETE_TABLES = (
    ("thread_dynamic_tools", "thread_id"),
    ("thread_spawn_edges", "parent_thread_id"),
    ("thread_spawn_edges", "child_thread_id"),
    ("stage1_outputs", "thread_id"),
    ("thread_goals", "thread_id"),
    ("logs", "thread_id"),
    ("jobs", "job_key"),
    ("threads", "id"),
)
STATE_CLEAR_TABLES = (
    ("agent_job_items", "assigned_thread_id"),
    ("jobs", "worker_id"),
)
GOAL_DELETE_TABLES = (("thread_goals", "thread_id"),)
LOG_DELETE_TABLES = (("logs", "thread_id"),)
MEMORY_DELETE_TABLES = (("stage1_outputs", "thread_id"), ("jobs", "job_key"))
APP_DELETE_TABLES = (("automation_runs", "thread_id"), ("inbox_items", "thread_id"))


@dataclass(frozen=True)
class _SqliteScrubPlan:
    delete_rows: tuple[tuple[str, str], ...] = ()
    clear_values: tuple[tuple[str, str], ...] = ()
    clear_state_text_references: bool = False


SQLITE_HOME_SCRUB_TARGETS = (
    (state_db_paths, _SqliteScrubPlan(STATE_DELETE_TABLES, STATE_CLEAR_TABLES, True)),
    (goals_db_paths, _SqliteScrubPlan(GOAL_DELETE_TABLES)),
    (logs_db_paths, _SqliteScrubPlan(LOG_DELETE_TABLES)),
    (memories_db_paths, _SqliteScrubPlan(MEMORY_DELETE_TABLES)),
)
CODEX_HOME_SCRUB_TARGETS = (("sqlite/*.db", _SqliteScrubPlan(APP_DELETE_TABLES)),)


def scrub_sqlite_files(home: Path, thread_ids: Iterable[str]) -> dict[Path, int]:
    thread_ids = tuple(thread_ids)
    changed_rows: dict[Path, int] = {}

    for sqlite_path, plan in _sqlite_scrub_targets(home):
        if not is_filelike(sqlite_path):
            continue

        count = _scrub_sqlite_rows(sqlite_path, plan=plan, thread_ids=thread_ids)
        if count:
            changed_rows[sqlite_path] = count

    return changed_rows


def delete_thread_artifacts(codex_home: Path, thread_ids: Iterable[str]) -> list[Path]:
    thread_ids = tuple(thread_ids)
    paths = set(_thread_file_artifact_paths(codex_home, thread_ids))
    paths.update(_scrub_memory_artifacts(codex_home, thread_ids))

    return _delete_paths(paths)


def _sqlite_scrub_targets(
    codex_home: Path,
) -> Iterable[tuple[Path, _SqliteScrubPlan]]:
    db_home = sqlite_home(codex_home)

    for path_finder, plan in SQLITE_HOME_SCRUB_TARGETS:
        for path in path_finder(db_home):
            yield path, plan

    for glob_pattern, plan in CODEX_HOME_SCRUB_TARGETS:
        for path in sorted(codex_home.glob(glob_pattern)):
            yield path, plan


def _scrub_sqlite_rows(
    path: Path,
    *,
    plan: _SqliteScrubPlan,
    thread_ids: tuple[str, ...],
) -> int:
    if not thread_ids:
        return 0

    placeholders = ", ".join("?" for _ in thread_ids)
    changed_rows = 0
    with sqlite3.connect(path, timeout=2) as connection:
        connection.execute("PRAGMA secure_delete = ON")
        columns_by_table: dict[str, set[str]] = {}

        for table, column in plan.delete_rows:
            changed_rows += _delete_matching_rows(
                connection, columns_by_table, table, column, placeholders, thread_ids
            )

        for table, column in plan.clear_values:
            changed_rows += _clear_matching_values(
                connection, columns_by_table, table, column, placeholders, thread_ids
            )

        if plan.clear_state_text_references:
            changed_rows += _clear_state_text_references(
                connection, columns_by_table, thread_ids
            )
        if changed_rows:
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return changed_rows


def _delete_matching_rows(
    connection: sqlite3.Connection,
    columns_by_table: dict[str, set[str]],
    table: str,
    column: str,
    placeholders: str,
    thread_ids: tuple[str, ...],
) -> int:
    if not _has_column(connection, columns_by_table, table, column):
        return 0

    cursor = connection.execute(
        f"DELETE FROM {quote_identifier(table)} "
        f"WHERE {quote_identifier(column)} IN ({placeholders})",
        thread_ids,
    )
    return max(cursor.rowcount, 0)


def _clear_matching_values(
    connection: sqlite3.Connection,
    columns_by_table: dict[str, set[str]],
    table: str,
    column: str,
    placeholders: str,
    thread_ids: tuple[str, ...],
) -> int:
    if not _has_column(connection, columns_by_table, table, column):
        return 0

    quoted_column = quote_identifier(column)
    cursor = connection.execute(
        f"UPDATE {quote_identifier(table)} SET {quoted_column} = NULL "
        f"WHERE {quoted_column} IN ({placeholders})",
        thread_ids,
    )
    return max(cursor.rowcount, 0)


def _has_column(
    connection: sqlite3.Connection,
    columns_by_table: dict[str, set[str]],
    table: str,
    column: str,
) -> bool:
    if table not in columns_by_table:
        columns_by_table[table] = sqlite_columns(connection, table)
    return column in columns_by_table[table]


def _clear_state_text_references(
    connection: sqlite3.Connection,
    columns_by_table: dict[str, set[str]],
    thread_ids: tuple[str, ...],
) -> int:
    columns_by_table.setdefault(
        "backfill_state", sqlite_columns(connection, "backfill_state")
    )
    if "last_watermark" not in columns_by_table["backfill_state"]:
        return 0

    column = quote_identifier("last_watermark")
    filters = " OR ".join(f"{column} LIKE ?" for _ in thread_ids)
    cursor = connection.execute(
        f"UPDATE {quote_identifier('backfill_state')} "  # noqa: S608
        f"SET {column} = NULL WHERE {filters}",
        tuple(f"%{thread_id}%" for thread_id in thread_ids),
    )
    return max(cursor.rowcount, 0)


def _thread_file_artifact_paths(
    codex_home: Path, thread_ids: tuple[str, ...]
) -> list[Path]:
    paths = set(_state_rollout_paths(codex_home, thread_ids))
    paths.update(_matching_files(codex_home, thread_ids))
    paths.update(_rollout_trace_bundle_paths(thread_ids))
    return sorted(paths)


def _state_rollout_paths(codex_home: Path, thread_ids: tuple[str, ...]) -> list[Path]:
    if not thread_ids:
        return []

    paths: list[Path] = []
    placeholders = ", ".join("?" for _ in thread_ids)
    query = f"SELECT rollout_path FROM threads WHERE id IN ({placeholders})"  # noqa: S608

    for sqlite_path in state_db_paths(sqlite_home(codex_home)):
        if not is_filelike(sqlite_path):
            continue

        with sqlite3.connect(sqlite_path, timeout=2) as connection:
            if not {"id", "rollout_path"} <= sqlite_columns(connection, "threads"):
                continue

            for (raw_path,) in connection.execute(query, thread_ids):
                if path := delete_path_candidate(codex_home, raw_path):
                    paths.append(path)

    return [path for path in paths if is_filelike(path)]


def _matching_files(codex_home: Path, thread_ids: tuple[str, ...]) -> list[Path]:
    return sorted(
        {
            path
            for thread_id in thread_ids
            for path in codex_home.rglob(f"*{glob_escape(thread_id)}*")
            if is_filelike(path)
        }
    )


def _memory_artifact_paths(
    codex_home: Path, thread_ids: tuple[str, ...]
) -> tuple[list[Path], tuple[str, ...], bool]:
    memory_root = codex_home / "memories"
    if memory_root.is_symlink():
        return [], (), False

    paths: list[Path] = []
    markers: set[str] = set()
    for path in (memory_root / "rollout_summaries").glob("*.md"):
        if not _file_mentions_any_marker(path, thread_ids):
            continue
        paths.append(path)
        markers.add(path.name)
        markers.update(_memory_file_markers(path))

    workspace_diff = memory_root / "phase2_workspace_diff.md"
    if _file_mentions_any_marker(workspace_diff, (*thread_ids, *markers)):
        paths.append(workspace_diff)

    return paths, tuple(markers), bool(paths)


def _scrub_memory_artifacts(
    codex_home: Path, thread_ids: tuple[str, ...]
) -> list[Path]:
    memory_root = codex_home / "memories"
    paths, memory_markers, memory_changed = _memory_artifact_paths(
        codex_home, thread_ids
    )

    raw_changed, raw_markers = _prune_raw_memories(
        memory_root / "raw_memories.md", thread_ids
    )
    markers = (*thread_ids, *memory_markers, *raw_markers)

    memory_changed |= raw_changed
    memory_changed |= _prune_lines_with_any_marker(memory_root / "MEMORY.md", markers)
    memory_changed |= _prune_lines_with_any_marker(
        memory_root / "memory_summary.md", markers
    )

    if memory_changed:
        paths.extend(_memory_git_history_paths(memory_root))
    return paths


def _memory_git_history_paths(memory_root: Path) -> list[Path]:
    if memory_root.is_symlink():
        return []

    git_dir = memory_root / ".git"
    return [git_dir] if git_dir.exists() or git_dir.is_symlink() else []


def _rollout_trace_bundle_paths(thread_ids: tuple[str, ...]) -> list[Path]:
    trace_root = os.environ.get("CODEX_ROLLOUT_TRACE_ROOT", "").strip()
    if not trace_root:
        return []

    root = Path(trace_root).expanduser()
    if not root.is_dir():
        return []

    return [
        path
        for path in root.iterdir()
        if path.name.startswith("trace-")
        and (path.is_dir() or path.is_symlink())
        and _trace_bundle_mentions_any_thread(path, thread_ids)
    ]


def _trace_bundle_mentions_any_thread(path: Path, thread_ids: tuple[str, ...]) -> bool:
    if any(thread_id in path.name for thread_id in thread_ids):
        return True

    files = [path / "manifest.json", path / "state.json", path / "trace.jsonl"]
    payloads = path / "payloads"
    if payloads.is_dir():
        files.extend(item for item in payloads.iterdir() if item.is_file())

    return any(_file_mentions_any_marker(file, thread_ids) for file in files)


def _delete_paths(paths: Iterable[Path]) -> list[Path]:
    deleted: list[Path] = []
    for path in sorted(set(paths), key=lambda item: len(item.parts), reverse=True):
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        deleted.append(path)
    return deleted


def _prune_raw_memories(
    path: Path, thread_ids: tuple[str, ...]
) -> tuple[bool, tuple[str, ...]]:
    if not is_filelike(path):
        return False, ()

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    output: list[str] = []
    skipping = removed = False
    markers: set[str] = set()

    for line in lines:
        if line.startswith("## Thread `"):
            skipping = any(
                f"## Thread `{thread_id}`" in line for thread_id in thread_ids
            )
            removed = removed or skipping
        if skipping:
            markers.update(_metadata_markers(line))
        if not skipping:
            output.append(line)

    if removed:
        write_text_atomically(path, "".join(output))
    return removed, tuple(markers)


def _prune_lines_with_any_marker(path: Path, markers: tuple[str, ...]) -> bool:
    if not markers or not is_filelike(path):
        return False

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    output = [line for line in lines if not _line_mentions_any_marker(line, markers)]
    if len(output) == len(lines):
        return False

    write_text_atomically(path, "".join(output))
    return True


def _line_mentions_any_marker(line: str, markers: Iterable[str]) -> bool:
    return any(marker in line for marker in markers)


def _memory_file_markers(path: Path) -> tuple[str, ...]:
    if not is_filelike(path):
        return ()

    try:
        return tuple(
            marker
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for marker in _metadata_markers(line)
        )
    except OSError:
        return ()


def _metadata_markers(line: str) -> tuple[str, ...]:
    key, separator, value = line.partition(":")
    if separator and key in {"rollout_path", "rollout_summary_file"}:
        return (value.strip(),)
    return ()


def _file_mentions_any_marker(path: Path, markers: tuple[str, ...]) -> bool:
    if not markers or not is_filelike(path):
        return False

    try:
        with path.open(encoding="utf-8", errors="ignore") as file:
            return any(marker in line for line in file for marker in markers)
    except OSError:
        return False
