# Codex Scrub Checkpoint

Use this when the `codex` submodule moves and the Python scrubber needs to be
checked against upstream Codex state storage.

## Fresh-Thread Procedure

1. Read this file first.
2. Compare the upstream anchor paths below against the updated `codex`
   submodule.
3. Treat anchor paths as current starting points, not an exhaustive whitelist. If
   a path moved or disappeared, search the submodule for the objective named by
   that path.
4. Compare every changed DB filename, migration, table, and thread-linked column
   against the Python owner functions below.
5. Add new scrub targets without removing old ones. This tool intentionally
   supports old and new Codex storage layouts at the same time.
6. Run:
   - `uv run ruff format .`
   - `uv run ruff format --check .`
   - `uv run ruff check .`
   - `uv run ty check`

## Exploration Scope

The objective is broader than any one file path: find every persistent place
where Codex can store a thread ID, thread-derived file path, rollout transcript,
memory, log, goal, subagent edge, job assignment, archive marker, or app-side
thread reference.

When upstream changes, search for concepts as well as files:

- DB definitions and locations: `DB_FILENAME`, `RuntimeDbSpec`, `RUNTIME_DBS`,
  `SQLITE_HOME_ENV`, `sqlite_home`, `sqlx::migrate`.
- Schema changes: `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`, `thread_id`,
  `rollout_path`, `parent_thread_id`, `child_thread_id`, `assigned_thread_id`,
  `worker_id`, `job_key`, `last_watermark`.
- Thread lifecycle: `delete_thread`, `mark_archived`, `mark_unarchived`,
  `ThreadMetadata`, `thread_spawn_edges`.
- File artifacts: `session_index`, `history.jsonl`, `sessions`,
  `archived_sessions`, `memories`, `rollout_summaries`,
  `CODEX_ROLLOUT_TRACE_ROOT`.
- Split stores: logs, goals, memories, agent jobs, backfill, app/desktop local
  SQLite stores.

## Current Anchor Paths

- `codex/codex-rs/state/src/lib.rs`: runtime DB filenames and public state APIs.
- `codex/codex-rs/state/src/runtime.rs`: DB open list and sqlite home usage.
- `codex/codex-rs/state/src/migrations.rs`: maps runtime DBs to migration dirs.
- `codex/codex-rs/state/migrations/*.sql`: legacy/main `state_*.sqlite` schema.
- `codex/codex-rs/state/logs_migrations/*.sql`: `logs_*.sqlite` schema.
- `codex/codex-rs/state/goals_migrations/*.sql`: `goals_*.sqlite` schema.
- `codex/codex-rs/state/memory_migrations/*.sql`: `memories_*.sqlite` schema.
- `codex/codex-rs/state/src/model/*.rs`: row/column semantics when migrations
  are not self-explanatory.
- `codex/codex-rs/state/src/extract.rs`: rollout item to thread metadata
  extraction, useful when thread metadata columns change.
- `codex/codex-rs/state/src/runtime/threads.rs`: thread delete/archive behavior.
- `codex/codex-rs/state/src/runtime/logs.rs`: thread-linked log rows.
- `codex/codex-rs/state/src/runtime/goals.rs`: thread-linked goal rows.
- `codex/codex-rs/state/src/runtime/memories.rs`: thread-linked memory rows/jobs.
- `codex/codex-rs/state/src/runtime/agent_jobs.rs`: `agent_jobs` and
  `agent_job_items` thread references.
- `codex/codex-rs/state/src/runtime/backfill.rs`: `backfill_state`
  watermark behavior.
- `codex/codex-rs/core/src/config/mod.rs`: persisted `sqlite_home` config and
  runtime-only `CODEX_SQLITE_HOME` behavior.

These paths reflect the last checked submodule. If upstream moves code, keep the
same exploration scope and update this anchor list after finding the new homes.

## Python Ownership Map

- `src/codex_scrub/storage.py`
  - `sqlite_home()`: resolves DB home from `config.toml`, then `CODEX_HOME`.
  - `_configured_sqlite_home()`: reads `config.toml` `sqlite_home`; relative
    values are relative to `CODEX_HOME`.
  - `state_db_paths()`, `logs_db_paths()`, `goals_db_paths()`,
    `memories_db_paths()`: find all versioned DBs matching
    `(state|logs|goals|memories)_*.sqlite`, not only the latest filenames.
- `src/codex_scrub/codex.py`
  - `load_threads()`: public thread-list entry point.
  - `_session_threads()`: loads legacy `session_index.jsonl`.
  - `_load_state_thread_map()` and `_load_state_threads()`: load `threads`
    rows from all state DBs.
  - `STATE_THREAD_COLUMNS`: state columns to keep compatible. It currently
    reads old `title` and new `preview`.
  - `scrub_thread()`: public scrub entry point. It rewrites JSONL, deletes file
    artifacts, and scrubs SQLite.
  - `_related_thread_ids()` and `_related_state_thread_ids()`: expand the
    requested thread to subagent/child thread IDs using `thread_spawn_edges`
    and rollout markers.
- `src/codex_scrub/cleanup.py`
  - `scrub_sqlite_files()`: public SQLite scrub entry point.
  - `_sqlite_scrub_targets()`: enumerates DB files and their scrub plans.
  - `_scrub_sqlite_rows()`: deletes/clears rows with `secure_delete` enabled
    and checkpoints WAL after changes.
  - `delete_thread_artifacts()`: public file-artifact delete entry point.
  - `_thread_file_artifact_paths()`: combines rollout paths, broad filename
    matches, and rollout trace bundles.
  - `_state_rollout_paths()`: reads `threads.rollout_path` from state DBs.
  - `_matching_files()`: scans all of `CODEX_HOME` for file/symlink names that
    contain any scrubbed thread ID.
  - `_scrub_memory_artifacts()`: prunes legacy markdown memory files.
  - `_rollout_trace_bundle_paths()`: finds trace bundles under
    `CODEX_ROLLOUT_TRACE_ROOT`.

## SQLite Tables Scrubbed

Legacy/main `state_*.sqlite`:

- Delete rows from `thread_dynamic_tools.thread_id`.
- Delete rows from `thread_spawn_edges.parent_thread_id`.
- Delete rows from `thread_spawn_edges.child_thread_id`.
- Delete rows from legacy `stage1_outputs.thread_id`.
- Delete rows from legacy `thread_goals.thread_id`.
- Delete rows from legacy `logs.thread_id`.
- Delete rows from legacy memory `jobs.job_key`.
- Delete rows from `threads.id`.
- Clear `agent_job_items.assigned_thread_id`.
- Clear legacy memory `jobs.worker_id`.
- Clear `backfill_state.last_watermark` when it references a thread ID.

Split DBs:

- `logs_*.sqlite`: delete `logs.thread_id`.
- `goals_*.sqlite`: delete `thread_goals.thread_id`.
- `memories_*.sqlite`: delete `stage1_outputs.thread_id` and stage-1
  `jobs.job_key`.

App DBs:

- `sqlite/*.db`: delete `automation_runs.thread_id` and `inbox_items.thread_id`
  when those tables exist.
- These tables are not currently defined under `codex/codex-rs/state`; treat
  them as app-side compatibility targets and keep them best-effort unless a
  newer app-side schema proves otherwise.

## File Artifacts Scrubbed

- `session_index.jsonl`: remove entries whose `id` is a scrubbed thread ID.
- `history.jsonl`: remove lines that mention any scrubbed thread ID.
- `threads.rollout_path`: delete the file/symlink path stored in every matching
  state DB row.
- Broad `CODEX_HOME` filename scan: delete any file/symlink whose name contains
  a scrubbed thread ID. This covers `sessions/`, `archived_sessions/`, and any
  other Codex-home file that embeds the ID in its filename.
- Legacy markdown memory files under `CODEX_HOME/memories`:
  - Delete `rollout_summaries/*.md` files that mention a scrubbed thread ID.
  - Prune matching sections from `raw_memories.md`.
  - Prune matching lines from `MEMORY.md` and `memory_summary.md`.
  - Delete `phase2_workspace_diff.md` when it mentions a scrubbed thread ID or
    a marker from a deleted rollout summary.
  - Delete `memories/.git` when any legacy memory file changed, so git history
    cannot retain scrubbed content.
- Rollout traces: under `CODEX_ROLLOUT_TRACE_ROOT`, delete `trace-*`
  directories/symlinks when the bundle name or contents mention a scrubbed
  thread ID. Contents checked include `manifest.json`, `state.json`,
  `trace.jsonl`, and direct files in `payloads/`.

## Latest Upstream State Checked

As of this checkpoint, upstream state defines:

- Submodule commit: `cdde711fac`
- `STATE_DB_FILENAME = "state_5.sqlite"`
- `LOGS_DB_FILENAME = "logs_2.sqlite"`
- `GOALS_DB_FILENAME = "goals_1.sqlite"`
- `MEMORIES_DB_FILENAME = "memories_1.sqlite"`

The main compatibility point is intentional: keep scrubbing old inline state
tables even when upstream has split them into dedicated DBs.

## Update Rules

- Do not stop at the anchor paths if search results show new stores or moved
  code. The anchor paths are there to focus exploration, not eliminate it.
- Do not add runtime-only environment lookup unless codex-scrub can reasonably
  observe the same value when launched by a user. Prefer persisted config such
  as `config.toml` for locating state.
- If upstream adds a new runtime DB prefix, add a finder in `storage.py`, add it
  to `VERSIONED_DB_RE`, and add a plan in `cleanup.py`.
- If upstream adds a new thread-linked table, add a delete or clear rule keyed
  by the exact thread column. Prefer deleting rows when the row only exists for
  that thread; prefer clearing values when the row is shared state.
- If upstream moves a table from `state_*.sqlite` into a split DB, keep the old
  state-table rule and add the new split-DB rule.
- If upstream adds file paths derived from thread metadata, add those paths to
  `delete_thread_artifacts()`.
- If upstream changes thread list metadata, update `STATE_THREAD_COLUMNS` and
  `_state_thread_from_row()` without breaking old columns.
