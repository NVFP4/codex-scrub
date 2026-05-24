# AGENTS.md

This is a Python TUI tool to scrub Codex threads from the disk.

## Commands

This project uses `uv`, `ruff` for linting and `ty` for type checker.

- `uv sync` to setup dependencies
- `uv run ruff format .` to format
- `uv run ruff format --check .` to check formatting.
- `uv run ty check` to check types.

## Code Guidelines

- PREFER simple, elegant, and readable Pythonic code.
- NEVER add pydantic or do any crazy type gymnastics.
- PREFER to keep files within 500+ LoC.
