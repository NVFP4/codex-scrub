from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from glob import escape as glob_escape
from pathlib import Path

from codex_scrub.cleanup import delete_thread_artifacts, scrub_sqlite_files
from codex_scrub.storage import (
    is_filelike,
    path_candidates,
    quote_identifier,
    sqlite_columns,
    sqlite_home,
    state_db_paths,
    write_text_atomically,
)

SESSION_INDEX = "session_index.jsonl"
HISTORY = "history.jsonl"
EPOCH = datetime.fromtimestamp(0, UTC)

STATE_THREAD_COLUMNS = (
    "id",
    "title",
    "preview",
    "updated_at_ms",
    "updated_at",
    "rollout_path",
    "cwd",
    "thread_source",
    "source",
    "archived",
    "archived_at",
    "tokens_used",
)
RELATED_THREAD_COLUMNS = ("id", "thread_source", "source", "rollout_path")


@dataclass(frozen=True)
class CodexThread:
    id: str
    name: str
    updated_at: datetime
    cwd: str | None = None
    is_zombie: bool = False
    is_archived: bool = False
    source: str = "app"
    tokens_used: int | None = None

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
    state_threads = _load_state_thread_map(sqlite_home(home))

    threads: dict[str, CodexThread] = {}
    for thread in _session_threads(home / SESSION_INDEX):
        state_thread = state_threads.get(thread.id)
        if state_thread and state_thread.is_subagent:
            continue
        threads[thread.id] = _merge_session_thread(home, thread, state_thread)

    for thread_id, state_thread in state_threads.items():
        if thread_id in threads or state_thread.is_subagent:
            continue
        if not _thread_has_trace_file(home, thread_id, state_thread.rollout_path):
            continue

        threads[thread_id] = replace(
            state_thread.thread,
            is_archived=_state_thread_is_archived(home, state_thread),
            is_zombie=_state_only_thread_is_zombie(state_thread.thread),
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
    deleted_files = tuple(delete_thread_artifacts(home, thread_ids))
    return ScrubResult(
        thread_id=thread_id,
        scrubbed_thread_ids=thread_ids,
        deleted_files=deleted_files,
        removed_jsonl_lines={
            path: count for path, count in removed_jsonl_lines.items() if count
        },
        changed_sqlite_rows=scrub_sqlite_files(home, thread_ids),
    )


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
        cwd=_thread_cwd(data.get("cwd")),
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


def _load_state_thread_map(db_home: Path) -> dict[str, _StateThread]:
    threads: dict[str, _StateThread] = {}
    for sqlite_path in state_db_paths(db_home):
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
        columns = sqlite_columns(connection, "threads")
        if "id" not in columns:
            return []

        rows = connection.execute(
            f"SELECT {_select_columns(columns, STATE_THREAD_COLUMNS)} "
            f"FROM {quote_identifier('threads')}"
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
        preview,
        updated_at_ms,
        updated_at,
        rollout_path,
        cwd,
        thread_source,
        source,
        archived,
        archived_at,
        tokens_used,
    ) = row
    if not isinstance(thread_id, str) or not thread_id:
        return None

    return _StateThread(
        thread=CodexThread(
            id=thread_id,
            name=_thread_name(title, preview),
            updated_at=_sqlite_updated_at(updated_at_ms, updated_at),
            cwd=_thread_cwd(cwd),
            source=_source_name(source),
            is_archived=_is_archived(archived, archived_at),
            tokens_used=_tokens_used(tokens_used),
        ),
        rollout_path=rollout_path,
        is_subagent=_is_subagent_thread(thread_source, source),
    )


def _thread_name(*values: object) -> str:
    name = next(
        (
            line.strip()
            for value in values
            if isinstance(value, str)
            for line in value.splitlines()
            if line.strip()
        ),
        "(untitled)",
    )
    if len(name) > 50:
        return name[:50] + "..."
    return name


def _thread_cwd(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    cwd = value.strip()
    return cwd or None


def _source_name(source: object) -> str:
    if source == "cli":
        return "cli"
    if source in {"vscode", "app", "desktop"}:
        return "app"
    return "unknown"


def _tokens_used(value: object) -> int | None:
    if not isinstance(value, int) or value < 0:
        return None
    return value


def _merge_session_thread(
    codex_home: Path, thread: CodexThread, state_thread: _StateThread | None
) -> CodexThread:
    if state_thread is None:
        return replace(thread, is_archived=_has_archived_rollout(codex_home, thread.id))

    return replace(
        thread,
        updated_at=state_thread.thread.updated_at,
        is_archived=_state_thread_is_archived(codex_home, state_thread),
        cwd=thread.cwd or state_thread.thread.cwd,
        source=state_thread.thread.source,
        tokens_used=state_thread.thread.tokens_used,
    )


def _state_thread_is_archived(codex_home: Path, state_thread: _StateThread) -> bool:
    return state_thread.thread.is_archived or _rollout_path_is_archived(
        codex_home, state_thread.rollout_path
    )


def _state_only_thread_is_zombie(thread: CodexThread) -> bool:
    return thread.source != "cli"


def _related_thread_ids(codex_home: Path, thread_id: str) -> tuple[str, ...]:
    related_ids = {thread_id}
    db_home = sqlite_home(codex_home)

    while True:
        discovered_ids = set(related_ids)
        for sqlite_path in state_db_paths(db_home):
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
        edge_columns = sqlite_columns(connection, "thread_spawn_edges")
        if {"parent_thread_id", "child_thread_id"} <= edge_columns:
            rows = connection.execute(
                f"SELECT {quote_identifier('parent_thread_id')}, "
                f"{quote_identifier('child_thread_id')} "
                f"FROM {quote_identifier('thread_spawn_edges')}"
            ).fetchall()
            related_ids.update(
                child_id
                for parent_id, child_id in rows
                if parent_id in parent_thread_ids and isinstance(child_id, str)
            )

        columns = sqlite_columns(connection, "threads")
        if "id" not in columns:
            return related_ids

        rows = connection.execute(
            f"SELECT {_select_columns(columns, RELATED_THREAD_COLUMNS)} "
            f"FROM {quote_identifier('threads')}"
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
        for path in path_candidates(codex_home, rollout_path)
    )


def _file_contains_any_marker(path: Path, markers: tuple[str, ...]) -> bool:
    try:
        with path.open(encoding="utf-8") as file:
            return any(marker in line for line in file for marker in markers)
    except OSError:
        return False


def _select_columns(columns: set[str], names: Iterable[str]) -> str:
    return ", ".join(
        quote_identifier(name) if name in columns else "NULL" for name in names
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


def _is_archived(archived: object, archived_at: object) -> bool:
    return _sqlite_bool(archived) or _sqlite_present(archived_at)


def _sqlite_present(value: object) -> bool:
    return value is not None and value != ""


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
    if any(is_filelike(path) for path in path_candidates(codex_home, rollout_path)):
        return True

    return any(
        is_filelike(path)
        for directory in ("sessions", "archived_sessions")
        for path in (codex_home / directory).rglob(f"*{glob_escape(thread_id)}*")
    )


def _has_archived_rollout(codex_home: Path, thread_id: str) -> bool:
    return any(
        is_filelike(path)
        for path in (codex_home / "archived_sessions").rglob(
            f"*{glob_escape(thread_id)}*"
        )
    )


def _rollout_path_is_archived(codex_home: Path, rollout_path: object) -> bool:
    archived_dir = codex_home / "archived_sessions"
    return any(
        path == archived_dir or archived_dir in path.parents
        for path in path_candidates(codex_home, rollout_path)
    )


def _rewrite_jsonl(path: Path, keep_line: Callable[[str], bool]) -> int:
    if not path.exists():
        return 0

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept_lines = [line for line in lines if keep_line(line)]
    removed_count = len(lines) - len(kept_lines)
    if removed_count:
        write_text_atomically(path, "".join(kept_lines))
    return removed_count


def _session_index_line_matches_any(line: str, thread_ids: Iterable[str]) -> bool:
    return _json_dict(line).get("id") in thread_ids


def _line_mentions_any_thread(line: str, thread_ids: Iterable[str]) -> bool:
    return any(thread_id in line for thread_id in thread_ids)
