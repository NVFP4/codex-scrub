from __future__ import annotations

from codex_scrub.tui import ScrubApp


def main() -> None:
    ScrubApp().run(mouse=False)
