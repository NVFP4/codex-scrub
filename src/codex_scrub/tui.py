from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from codex_scrub.codex import (
    CodexThread,
    default_codex_home,
    load_threads,
    scrub_thread,
)

TITLE = "\n".join(
    (
        "              __                             __ ",
        " _______  ___/ /____ __    ___ __________ __/ / ",
        "/ __/ _ \\/ _  / -_) \\ /   (_-</ __/ __/ // / _ \\",
        "\\__/\\___/\\_,_/\\__/_\\_\\   /___/\\__/_/  \\_,_/_.__/",
    )
)
TITLE_COMPACT = "Codex Scrub"
TITLE_MIN_WIDTH = 70


class ThreadListItem(ListItem):
    def __init__(self, thread: CodexThread) -> None:
        self.thread = thread
        super().__init__(
            Static(_thread_text(thread), markup=False),
            classes="thread-row",
        )


class HeaderItem(ListItem):
    def __init__(self, label: str, classes: str = "date-header") -> None:
        super().__init__(
            Static(Text(label, style="bold"), markup=False),
            classes=classes,
            disabled=True,
        )


class SpacerItem(ListItem):
    def __init__(self) -> None:
        super().__init__(Static(""), disabled=True)


class ConfirmDeleteScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
        Binding("space", "activate", "Close", show=False),
        Binding("up,down,j,k", "noop", "Ignore", show=False),
    ]

    def __init__(self, thread: CodexThread) -> None:
        super().__init__()
        self.thread = thread
        self.is_deleting = False
        self.is_finished = False

    def compose(self) -> ComposeResult:
        with Container(id="confirm-dialog"):
            yield Static("Permanently delete everything?", id="confirm-title")
            yield Static(self.thread.name, id="confirm-name")
            yield Static("enter confirm   n/escape cancel", id="confirm-help")

    async def action_confirm(self) -> None:
        if self.is_finished:
            self.dismiss(None)
            return

        if self.is_deleting or self.is_finished:
            return

        app = self.app
        assert isinstance(app, ScrubApp)

        self.is_deleting = True
        self.query_one("#confirm-title", Static).update("Deleting thread...")
        self.query_one("#confirm-help", Static).update("")
        success, message = await app._delete_thread(self.thread)
        self.is_deleting = False
        self.is_finished = True
        self.query_one("#confirm-title", Static).update(
            "Deleted thread" if success else "Could not delete thread"
        )
        self.query_one("#confirm-name", Static).update(message)
        self.query_one("#confirm-help", Static).update("enter/space/escape close")

    def action_cancel(self) -> None:
        if self.is_deleting:
            return

        if not self.is_finished:
            app = self.app
            assert isinstance(app, ScrubApp)
            app._clear_confirmation()
        self.dismiss(None)

    def action_activate(self) -> None:
        if self.is_finished:
            self.dismiss(None)

    def action_noop(self) -> None:
        pass


class ScrubApp(App[None]):
    CSS = """
    App {
        background: ansi_default;
    }

    Screen {
        background: ansi_default;
        color: ansi_default;
        padding: 1 1;
    }

    #title {
        height: 5;
        padding: 0 1;
        content-align: left top;
        text-style: bold;
        background: transparent;
    }

    #title.compact-title {
        height: 2;
        content-align: left middle;
    }

    #thread-list {
        height: 1fr;
        background: transparent;
    }

    .date-header,
    .group-header {
        height: 1;
        padding: 0 1;
        color: ansi_bright_cyan;
        text-style: bold;
        background: transparent;
    }

    .group-header {
        background: transparent;
    }

    .thread-row {
        height: 1;
        padding: 0 1;
        background: transparent;
    }

    .thread-row > Static {
        width: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }

    #thread-list > ListItem.-hovered {
        background: transparent;
    }

    #thread-list > ListItem.-highlight {
        background: ansi_bright_black 45%;
        color: $text;
        text-style: bold;
    }

    #thread-list:focus {
        background-tint: transparent;
    }

    #thread-list:focus > ListItem.-highlight {
        background: ansi_bright_black 45%;
        color: $text;
        text-style: bold;
    }

    .pending-delete {
        background: ansi_red;
        color: ansi_default;
        text-style: bold;
    }

    #help,
    #status {
        height: 1;
        padding: 0 1;
        color: ansi_bright_black;
        background: transparent;
    }

    #help {
        color: ansi_default;
    }

    #status.error {
        color: ansi_red;
        text-style: bold;
    }

    ConfirmDeleteScreen {
        align: center middle;
        color: ansi_default;
    }

    #confirm-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round ansi_red;
        background: ansi_default;
    }

    #confirm-title {
        height: 1;
        color: ansi_red;
        text-style: bold;
    }

    #confirm-name {
        height: auto;
        margin: 1 0;
    }

    #confirm-help {
        height: 1;
        color: ansi_bright_black;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("j", "cursor_down", "move down", key_display="j/down"),
        Binding("k", "cursor_up", "move up", key_display="k/up"),
        Binding("enter", "select_thread", "delete"),
    ]

    def __init__(self, codex_home: Path | None = None) -> None:
        super().__init__(ansi_color=True)
        self.codex_home = codex_home or default_codex_home()
        self.threads: list[CodexThread] = []
        self.confirming_thread_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(TITLE, markup=False, id="title")
        yield ListView(id="thread-list")
        yield Static("", id="status")
        yield Static(_help_text(), id="help")

    async def on_mount(self) -> None:
        self._refresh_title(self.size.width)
        await self._reload_threads()
        self.query_one("#thread-list", ListView).focus()

    def on_resize(self, event: events.Resize) -> None:
        self._refresh_title(event.size.width)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ThreadListItem):
            await self._choose_thread(event.item.thread)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if not self.confirming_thread_id:
            return

        thread_id = (
            event.item.thread.id if isinstance(event.item, ThreadListItem) else None
        )
        if thread_id != self.confirming_thread_id:
            self._clear_confirmation()

    def action_cursor_down(self) -> None:
        self.query_one("#thread-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#thread-list", ListView).action_cursor_up()

    async def action_select_thread(self) -> None:
        item = self.query_one("#thread-list", ListView).highlighted_child
        if isinstance(item, ThreadListItem):
            await self._choose_thread(item.thread)

    async def _choose_thread(self, thread: CodexThread) -> None:
        self.confirming_thread_id = thread.id
        self._mark_pending(thread.id)
        self.push_screen(ConfirmDeleteScreen(thread))

    async def _delete_thread(self, thread: CodexThread) -> tuple[bool, str]:
        self.confirming_thread_id = None
        self._set_status(f"Deleting {thread.name}...")

        try:
            result = scrub_thread(thread.id, self.codex_home)
        except (OSError, sqlite3.Error, ValueError) as error:
            self._clear_confirmation(update_status=False)
            message = f"Failed deleting {thread.name}: {error}"
            self._set_status(message, error=True)
            return False, message

        await self._reload_threads(
            keep_thread_id=self._neighbor_thread_id(result.scrubbed_thread_ids)
        )
        self._set_status(self._thread_count_message())
        return True, (
            f"Deleted {thread.name}: {result.file_count} files, "
            f"{result.sqlite_row_count} sqlite rows, {result.jsonl_line_count} jsonl lines"
            f", {result.related_thread_count} attached threads."
        )

    async def _reload_threads(self, keep_thread_id: str | None = None) -> None:
        self.threads = load_threads(self.codex_home)

        thread_list = self.query_one("#thread-list", ListView)
        await thread_list.clear()

        items = self._list_items()
        if items:
            await thread_list.extend(items)
            thread_list.index = self._index_for_thread(items, keep_thread_id)
        else:
            thread_list.index = None

        self._clear_confirmation(update_status=False)
        if not self.threads:
            self._set_status(f"No threads found in {self.codex_home}.")
        elif keep_thread_id is None:
            self._set_status(self._thread_count_message())

    def _list_items(self) -> list[ListItem]:
        active_threads = [thread for thread in self.threads if not thread.is_archived]
        archived_threads = [thread for thread in self.threads if thread.is_archived]
        items: list[ListItem] = [HeaderItem("Active Threads", classes="group-header")]
        items.extend(ThreadListItem(thread) for thread in active_threads)
        if archived_threads:
            items.extend(
                [SpacerItem(), HeaderItem("Archived Threads", classes="group-header")]
            )
            items.extend(ThreadListItem(thread) for thread in archived_threads)
        return items

    def _neighbor_thread_id(self, deleted_thread_ids: Iterable[str]) -> str | None:
        deleted_thread_ids = set(deleted_thread_ids)
        for index, thread in enumerate(self.threads):
            if thread.id not in deleted_thread_ids:
                continue

            neighbors = [*self.threads[index + 1 :], *reversed(self.threads[:index])]
            return next(
                (
                    neighbor.id
                    for neighbor in neighbors
                    if neighbor.id not in deleted_thread_ids
                ),
                None,
            )
        return None

    def _index_for_thread(
        self, items: list[ListItem], keep_thread_id: str | None
    ) -> int | None:
        thread_indexes = [
            (index, item.thread.id)
            for index, item in enumerate(items)
            if isinstance(item, ThreadListItem)
        ]
        return next(
            (
                index
                for index, thread_id in thread_indexes
                if thread_id == keep_thread_id
            ),
            thread_indexes[0][0] if thread_indexes else None,
        )

    def _mark_pending(self, thread_id: str) -> None:
        for item in self.query(ThreadListItem):
            item.set_class(item.thread.id == thread_id, "pending-delete")

    def _clear_confirmation(self, update_status: bool = True) -> None:
        self.confirming_thread_id = None
        for item in self.query(ThreadListItem):
            item.remove_class("pending-delete")

        if update_status:
            self._set_status(self._thread_count_message())

    def _thread_count_message(self) -> str:
        archived_count = sum(thread.is_archived for thread in self.threads)
        active_count = len(self.threads) - archived_count
        return (
            f'{active_count} active, {archived_count} archived in "{self.codex_home}"'
        )

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.update(message)
        status.set_class(error, "error")

    def _refresh_title(self, width: int) -> None:
        compact = width < TITLE_MIN_WIDTH
        title = self.query_one("#title", Static)
        title.update(TITLE_COMPACT if compact else TITLE)
        title.set_class(compact, "compact-title")


def _thread_text(thread: CodexThread) -> Text:
    updated_at = thread.local_updated_at.strftime("%Y-%m-%d %H:%M")
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{updated_at}  ", style="yellow")
    text.append(f"{thread.source.upper():<3}", style="yellow")
    text.append("  ")
    text.append(thread.name, style="white bold")
    if thread.is_zombie:
        text.append("  zombie", style="yellow bold")
    return text


def _help_text() -> Text:
    text = Text()
    shortcuts = (
        ("q", "quit"),
        ("j/down", "move down"),
        ("k/up", "move up"),
        ("enter", "delete"),
    )
    for index, (keys, label) in enumerate(shortcuts):
        if index:
            text.append("   ")
        text.append(keys, style="bright_black")
        text.append(f" {label}", style="dim bright_black")
    return text
