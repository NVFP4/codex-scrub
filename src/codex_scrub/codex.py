from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from glob import escape as glob_escape
from pathlib import Path

SESSION_INDEX = "session_index.jsonl"
HISTORY = "history.jsonl"
EPOCH = datetime.fromtimestamp(0, UTC)

STATE_DELETE_TABLES = (
    ("thread_dynamic_tools", "thread_id"),
    ("thread_spawn_edges", "parent_thread_id"),
    ("thread_spawn_edges", "child_thread_id"),
    ("stage1_outputs", "thread_id"),
    ("threads", "id"),
)
STATE_CLEAR_TABLES = (("agent_job_items", "assigned_thread_id"),)
GOAL_DELETE_TABLES = (("thread_goals", "thread_id"),)
LOG_DELETE_TABLES = (("logs", "thread_id"),)
APP_DELETE_TABLES = (("automation_runs", "thread_id"), ("inbox_items", "thread_id"))
SQLITE_SCRUB_TARGETS = (
    ("state_*.sqlite", STATE_DELETE_TABLES, STATE_CLEAR_TABLES),
    ("goals_*.sqlite", GOAL_DELETE_TABLES, ()),
    ("logs_*.sqlite", LOG_DELETE_TABLES, ()),
    ("sqlite/*.db", APP_DELETE_TABLES, ()),
)
STATE_THREAD_COLUMNS = (
    "id",
    "title",
    "updated_at_ms",
    "updated_at",
    "rollout_path",
    "thread_source",
    "source",
    "archived",
)
RELATED_THREAD_COLUMNS = ("id", "thread_source", "source", "rollout_path")


@dataclass(frozen=True)
class CodexThread:
    id: str
    name: str
    updated_at: datetime
    is_zombie: bool = False
    is_archived: bool = False
    source: str = "app"

    @property
    def local_updated_at(self) -> datetime:
        return self.updated_at.astimezone()


@dataclass(frozen=True)
class _StateThread:
    thread: CodexThread
    rollout_path: object
    is_subagent: bool


@dataclass(frozen=True)
class ScrubResult:
    thread_id: str
    scrubbed_thread_ids: tuple[str, ...]
    deleted_files: tuple[Path, ...]
    removed_jsonl_lines: dict[Path, int]
    changed_sqlite_rows: dict[Path, int]

    @property
    def file_count(self) -> int:
        return len(self.deleted_files)

    @property
    def jsonl_line_count(self) -> int:
        return sum(self.removed_jsonl_lines.values())

    @property
    def sqlite_row_count(self) -> int:
        return sum(self.changed_sqlite_rows.values())

    @property
    def related_thread_count(self) -> int:
        return max(len(self.scrubbed_thread_ids) - 1, 0)


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def load_threads(codex_home: Path | None = None) -> list[CodexThread]:
    home = _codex_home(codex_home)
    state_threads = _load_state_thread_map(home)
    threads = {
        thread.id: replace(
            thread,
            is_archived=bool(state_thread and state_thread.thread.is_archived),
            source=state_thread.thread.source if state_thread else thread.source,
        )
        for thread in _session_threads(home / SESSION_INDEX)
        if not (
            (state_thread := state_threads.get(thread.id)) and state_thread.is_subagent
        )
    }
    threads.update(
        {
            thread_id: replace(
                state_thread.thread,
                is_zombie=state_thread.thread.source != "cli",
            )
            for thread_id, state_thread in state_threads.items()
            if thread_id not in threads
            and not state_thread.is_subagent
            and _thread_has_trace_file(home, thread_id, state_thread.rollout_path)
        }
    )
    return sorted(
        threads.values(), key=lambda thread: thread.local_updated_at, reverse=True
    )


def scrub_thread(thread_id: str, codex_home: Path | None = None) -> ScrubResult:
    if not thread_id:
        raise ValueError("thread_id cannot be empty")

    home = _codex_home(codex_home)
    thread_ids = _related_thread_ids(home, thread_id)
    removed_jsonl_lines = {
        home / SESSION_INDEX: _rewrite_jsonl(
            home / SESSION_INDEX,
            lambda line: not _session_index_line_matches_any(line, thread_ids),
        ),
        home / HISTORY: _rewrite_jsonl(
            home / HISTORY,
            lambda line: not _line_mentions_any_thread(line, thread_ids),
        ),
    }
    return ScrubResult(
        thread_id=thread_id,
        scrubbed_thread_ids=thread_ids,
        deleted_files=tuple(_delete_matching_files(home, thread_ids)),
        removed_jsonl_lines={
            path: count for path, count in removed_jsonl_lines.items() if count
        },
        changed_sqlite_rows=_scrub_sqlite_files(home, thread_ids),
    )


def _scrub_sqlite_files(home: Path, thread_ids: Iterable[str]) -> dict[Path, int]:
    thread_ids = tuple(thread_ids)
    return {
        sqlite_path: count
        for glob_pattern, delete_rows, clear_values in SQLITE_SCRUB_TARGETS
        for sqlite_path in sorted(home.glob(glob_pattern))
        if (
            count := _scrub_sqlite_rows(
                sqlite_path,
                delete_rows=delete_rows,
                clear_values=clear_values,
                thread_ids=thread_ids,
            )
        )
    }


def _codex_home(codex_home: Path | None) -> Path:
    return (codex_home or default_codex_home()).expanduser()


def _session_threads(path: Path) -> list[CodexThread]:
    if not path.exists():
        return []
    return [
        thread
        for line in path.read_text(encoding="utf-8").splitlines()
        if (thread := _thread_from_jsonl(line)) is not None
    ]


def _thread_from_jsonl(line: str) -> CodexThread | None:
    data = _json_dict(line)
    if not isinstance(thread_id := data.get("id"), str) or not thread_id:
        return None
    return CodexThread(
        id=thread_id,
        name=_thread_name(data.get("thread_name")),
        updated_at=_parse_datetime(data.get("updated_at")),
    )


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        return EPOCH

    try:
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return EPOCH

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json_dict(line: str) -> dict[str, object]:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_state_thread_map(codex_home: Path) -> dict[str, _StateThread]:
    threads: dict[str, _StateThread] = {}
    for sqlite_path in sorted(codex_home.glob("state_*.sqlite")):
        try:
            state_threads = _load_state_threads(sqlite_path)
        except (OSError, sqlite3.Error):
            continue

        for state_thread in state_threads:
            existing = threads.get(state_thread.thread.id)
            if (
                existing is None
                or state_thread.thread.updated_at > existing.thread.updated_at
            ):
                threads[state_thread.thread.id] = state_thread

    return threads


def _load_state_threads(sqlite_path: Path) -> list[_StateThread]:
    with sqlite3.connect(sqlite_path, timeout=2) as connection:
        columns = _sqlite_columns(connection, "threads")
        if "id" not in columns:
            return []

        rows = connection.execute(
            f"SELECT {_select_columns(columns, STATE_THREAD_COLUMNS)} "
            f"FROM {_quote_identifier('threads')}"
        ).fetchall()

    return [
        state_thread
        for row in rows
        if (state_thread := _state_thread_from_row(row)) is not None
    ]


def _state_thread_from_row(row: tuple[object, ...]) -> _StateThread | None:
    (
        thread_id,
        title,
        updated_at_ms,
        updated_at,
        rollout_path,
        thread_source,
        source,
        archived,
    ) = row
    if not isinstance(thread_id, str) or not thread_id:
        return None

    return _StateThread(
        thread=CodexThread(
            id=thread_id,
            name=_thread_name(title),
            updated_at=_sqlite_updated_at(updated_at_ms, updated_at),
            source=_source_name(source),
            is_archived=_sqlite_bool(archived),
        ),
        rollout_path=rollout_path,
        is_subagent=_is_subagent_thread(thread_source, source),
    )


def _thread_name(title: object) -> str:
    return title if isinstance(title, str) and title else "(untitled)"


def _source_name(source: object) -> str:
    if source == "cli":
        return "cli"
    if source in {"vscode", "app", "desktop"}:
        return "app"
    return "unknown"


def _related_thread_ids(codex_home: Path, thread_id: str) -> tuple[str, ...]:
    related_ids = {thread_id}

    while True:
        discovered_ids = set(related_ids)
        for sqlite_path in sorted(codex_home.glob("state_*.sqlite")):
            try:
                discovered_ids.update(
                    _related_state_thread_ids(sqlite_path, codex_home, related_ids)
                )
            except (OSError, sqlite3.Error):
                continue

        if discovered_ids == related_ids:
            return tuple(sorted(related_ids))
        related_ids = discovered_ids


def _related_state_thread_ids(
    sqlite_path: Path, codex_home: Path, parent_thread_ids: set[str]
) -> set[str]:
    related_ids: set[str] = set()
    with sqlite3.connect(sqlite_path, timeout=2) as connection:
        edge_columns = _sqlite_columns(connection, "thread_spawn_edges")
        if {"parent_thread_id", "child_thread_id"} <= edge_columns:
            rows = connection.execute(
                f"SELECT {_quote_identifier('parent_thread_id')}, "
                f"{_quote_identifier('child_thread_id')} "
                f"FROM {_quote_identifier('thread_spawn_edges')}"
            ).fetchall()
            related_ids.update(
                child_id
                for parent_id, child_id in rows
                if parent_id in parent_thread_ids and isinstance(child_id, str)
            )

        columns = _sqlite_columns(connection, "threads")
        if "id" not in columns:
            return related_ids

        rows = connection.execute(
            f"SELECT {_select_columns(columns, RELATED_THREAD_COLUMNS)} "
            f"FROM {_quote_identifier('threads')}"
        ).fetchall()

    return related_ids | {
        thread_id
        for thread_id, thread_source, source, rollout_path in rows
        if isinstance(thread_id, str)
        and thread_id not in parent_thread_ids
        and _is_subagent_thread(thread_source, source)
        and _subagent_references_parent(codex_home, rollout_path, parent_thread_ids)
    }


def _is_subagent_thread(thread_source: object, source: object) -> bool:
    return thread_source == "subagent" or (
        isinstance(source, str) and "subagent" in source
    )


def _subagent_references_parent(
    codex_home: Path,
    rollout_path: object,
    parent_thread_ids: set[str],
) -> bool:
    markers = tuple(
        f"Reviewed Codex session id: {parent_thread_id}"
        for parent_thread_id in parent_thread_ids
    )
    return any(
        path.is_file() and _file_contains_any_marker(path, markers)
        for path in _path_candidates(codex_home, rollout_path)
    )


def _file_contains_any_marker(path: Path, markers: tuple[str, ...]) -> bool:
    try:
        with path.open(encoding="utf-8") as file:
            return any(marker in line for line in file for marker in markers)
    except OSError:
        return False


def _select_columns(columns: set[str], names: Iterable[str]) -> str:
    return ", ".join(
        _quote_identifier(name) if name in columns else "NULL" for name in names
    )


def _sqlite_updated_at(updated_at_ms: object, updated_at: object) -> datetime:
    return (
        _parse_epoch_datetime(updated_at_ms, milliseconds=True)
        or _parse_epoch_datetime(updated_at, milliseconds=False)
        or _parse_datetime(updated_at)
    )


def _sqlite_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return isinstance(value, bool | int | float) and bool(value)


def _parse_epoch_datetime(value: object, *, milliseconds: bool) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None

    try:
        timestamp = float(value)
    except ValueError:
        return None

    if milliseconds:
        timestamp /= 1000

    try:
        return datetime.fromtimestamp(timestamp, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _thread_has_trace_file(
    codex_home: Path, thread_id: str, rollout_path: object
) -> bool:
    if any(_is_filelike(path) for path in _path_candidates(codex_home, rollout_path)):
        return True

    return any(
        _is_filelike(path)
        for path in (codex_home / "sessions").rglob(f"*{glob_escape(thread_id)}*")
    )


def _path_candidates(codex_home: Path, value: object) -> tuple[Path, ...]:
    if not isinstance(value, str) or not value:
        return ()
    path = Path(value).expanduser()
    return (path,) if path.is_absolute() else (codex_home / path, path)


def _rewrite_jsonl(path: Path, keep_line: Callable[[str], bool]) -> int:
    if not path.exists():
        return 0

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept_lines = [line for line in lines if keep_line(line)]
    removed_count = len(lines) - len(kept_lines)
    if removed_count:
        _write_text_atomically(path, "".join(kept_lines))
    return removed_count


def _write_text_atomically(path: Path, content: str) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(path)


def _session_index_line_matches_any(line: str, thread_ids: Iterable[str]) -> bool:
    return _json_dict(line).get("id") in thread_ids


def _line_mentions_any_thread(line: str, thread_ids: Iterable[str]) -> bool:
    return any(thread_id in line for thread_id in thread_ids)


def _scrub_sqlite_rows(
    path: Path,
    *,
    delete_rows: tuple[tuple[str, str], ...],
    clear_values: tuple[tuple[str, str], ...],
    thread_ids: tuple[str, ...],
) -> int:
    if not thread_ids:
        return 0

    placeholders = ", ".join("?" for _ in thread_ids)
    changed_rows = 0
    with sqlite3.connect(path, timeout=2) as connection:
        columns_by_table: dict[str, set[str]] = {}
        for template, targets in (
            (
                f"DELETE FROM {{table}} WHERE {{column}} IN ({placeholders})",
                delete_rows,
            ),
            (
                f"UPDATE {{table}} SET {{column}} = NULL "
                f"WHERE {{column}} IN ({placeholders})",
                clear_values,
            ),
        ):
            for table, column in targets:
                if table not in columns_by_table:
                    columns_by_table[table] = _sqlite_columns(connection, table)
                columns = columns_by_table[table]
                if column not in columns:
                    continue

                cursor = connection.execute(
                    template.format(
                        table=_quote_identifier(table),
                        column=_quote_identifier(column),
                    ),
                    thread_ids,
                )
                changed_rows += max(cursor.rowcount, 0)

    return changed_rows


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    return {row[1] for row in rows if isinstance(row[1], str)}


def _quote_identifier(identifier: str) -> str:
    escaped_identifier = identifier.replace('"', '""')
    return f'"{escaped_identifier}"'


def _delete_matching_files(codex_home: Path, thread_ids: Iterable[str]) -> list[Path]:
    paths = sorted(
        {
            path
            for thread_id in thread_ids
            for path in codex_home.rglob(f"*{glob_escape(thread_id)}*")
            if _is_filelike(path)
        }
    )
    for path in paths:
        path.unlink()
    return paths


def _is_filelike(path: Path) -> bool:
    return path.is_file() or path.is_symlink()
