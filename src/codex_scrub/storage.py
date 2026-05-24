from __future__ import annotations

import re
import sqlite3
import tomllib
from pathlib import Path

VERSIONED_DB_RE = re.compile(r"^(state|goals|logs)_\d+\.sqlite$")


def sqlite_home(codex_home: Path) -> Path:
    return _configured_sqlite_home(codex_home) or codex_home


def _configured_sqlite_home(codex_home: Path) -> Path | None:
    try:
        data = tomllib.loads((codex_home / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None

    value = data.get("sqlite_home")
    if not isinstance(value, str) or not value:
        return None

    path = Path(value).expanduser()
    return path if path.is_absolute() else codex_home / path


def path_candidates(codex_home: Path, value: object) -> tuple[Path, ...]:
    if not isinstance(value, str) or not value:
        return ()
    path = Path(value).expanduser()
    return (path,) if path.is_absolute() else (codex_home / path, path)


def delete_path_candidate(codex_home: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = codex_home / path
    return path.resolve(strict=False)


def state_db_paths(db_home: Path) -> tuple[Path, ...]:
    return _versioned_db_paths(db_home, "state")


def goals_db_paths(db_home: Path) -> tuple[Path, ...]:
    return _versioned_db_paths(db_home, "goals")


def logs_db_paths(db_home: Path) -> tuple[Path, ...]:
    return _versioned_db_paths(db_home, "logs")


def _versioned_db_paths(db_home: Path, prefix: str) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in db_home.glob(f"{prefix}_*.sqlite")
            if VERSIONED_DB_RE.fullmatch(path.name)
        )
    )


def write_text_atomically(path: Path, content: str) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(path)


def sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(
        f"PRAGMA table_info({quote_identifier(table)})"
    ).fetchall()
    return {row[1] for row in rows if isinstance(row[1], str)}


def quote_identifier(identifier: str) -> str:
    escaped_identifier = identifier.replace('"', '""')
    return f'"{escaped_identifier}"'


def is_filelike(path: Path) -> bool:
    return path.is_file() or path.is_symlink()
