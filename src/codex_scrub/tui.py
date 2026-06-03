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
        self.label = Static(_thread_text(thread), markup=False)
        super().__init__(
            self.label,
            classes="thread-row",
        )

    def set_highlighted(self, highlighted: bool) -> None:
        archived_style = "black" if highlighted else "bright_black"
        self.label.update(_thread_text(self.thread, archived_style=archived_style))


class HeaderItem(ListItem):
    def __init__(self, label: str | Text, classes: str = "date-header") -> None:
        if isinstance(label, str):
            label = Text(label, style="bold")
        super().__init__(
            Static(label, markup=False),
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
        padding: 1 1 0 1;
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
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-color: ansi_bright_black;
        scrollbar-color-hover: ansi_white;
        scrollbar-color-active: ansi_white;
    }

    .date-header,
    .group-header,
    .cwd-header {
        height: 1;
        padding: 0 1;
        color: ansi_bright_cyan;
        text-style: bold;
        background: transparent;
    }

    .group-header {
        background: transparent;
    }

    .cwd-header {
        color: ansi_cyan;
        text-style: none;
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
        background: ansi_bright_black 20%;
        color: $text;
        text-style: bold;
    }

    #thread-list:focus {
        background-tint: transparent;
    }

    #thread-list:focus > ListItem.-highlight {
        background: ansi_bright_black 20%;
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

    #status {
        margin-top: 1;
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
        Binding("down,j", "cursor_down", "move down", show=False),
        Binding("up,k", "cursor_up", "move up", show=False),
        Binding("right,l", "next_dir", "next dir", show=False),
        Binding("left,h", "previous_dir", "previous dir", show=False),
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
        self._refresh_thread_markers(event.item)

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

    def action_next_dir(self) -> None:
        self._jump_dir(1)

    def action_previous_dir(self) -> None:
        self._jump_dir(-1)

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
            self._refresh_thread_markers(thread_list.highlighted_child)
        else:
            thread_list.index = None

        self._clear_confirmation(update_status=False)
        if not self.threads:
            self._set_status(f"No threads found in {self.codex_home}.")
        elif keep_thread_id is None:
            self._set_status(self._thread_count_message())

    def _list_items(self) -> list[ListItem]:
        return _thread_group_items(self.threads)

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

    def _jump_dir(self, direction: int) -> None:
        thread_list = self.query_one("#thread-list", ListView)
        items = list(thread_list.children)
        current_index = thread_list.index
        if current_index is None:
            return

        group_starts = [
            index
            for index, item in enumerate(items)
            if isinstance(item, ThreadListItem)
            and (index == 0 or not isinstance(items[index - 1], ThreadListItem))
        ]
        if not group_starts:
            return

        current_group = max(
            (
                index
                for index, start in enumerate(group_starts)
                if start <= current_index
            ),
            default=0,
        )
        target_group = current_group + direction
        if 0 <= target_group < len(group_starts):
            thread_list.index = group_starts[target_group]

    def _mark_pending(self, thread_id: str) -> None:
        for item in self.query(ThreadListItem):
            item.set_class(item.thread.id == thread_id, "pending-delete")

    def _refresh_thread_markers(self, highlighted_item: ListItem | None) -> None:
        for item in self.query(ThreadListItem):
            item.set_highlighted(item is highlighted_item)
        self._keep_group_header_visible(highlighted_item)

    def _keep_group_header_visible(self, highlighted_item: ListItem | None) -> None:
        if not isinstance(highlighted_item, ThreadListItem):
            return

        thread_list = self.query_one("#thread-list", ListView)
        items = list(thread_list.children)
        try:
            index = items.index(highlighted_item)
        except ValueError:
            return

        if index == 0 or isinstance(items[index - 1], ThreadListItem):
            return

        thread_list.call_after_refresh(
            thread_list.scroll_to_widget,
            items[index - 1],
            animate=False,
        )

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


def _thread_text(thread: CodexThread, *, archived_style: str = "bright_black") -> Text:
    updated_at = thread.local_updated_at.strftime("%Y-%m-%d %H:%M")
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{updated_at}  ", style="yellow")
    text.append(f"{thread.source.upper():<3}", style="yellow")
    text.append("  ")
    tokens = _human_tokens(thread.tokens_used) if thread.tokens_used is not None else ""
    text.append(f"{tokens:>11}  ", style="yellow")
    text.append(thread.name, style="white bold")
    if thread.is_archived:
        text.append("  [archived]", style=archived_style)
    if thread.is_zombie:
        text.append("  zombie", style="yellow bold")
    return text


def _human_tokens(tokens: int) -> str:
    if tokens < 1_000:
        return f"{tokens} toks"
    if tokens < 1_000_000:
        return f"{tokens // 1_000}K toks"

    value = tokens / 1_000_000
    formatted = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{formatted}M toks"


def _thread_group_items(threads: list[CodexThread]) -> list[ListItem]:
    items: list[ListItem] = []
    groups: dict[str, list[CodexThread]] = {}
    for thread in sorted(threads, key=lambda thread: thread.updated_at, reverse=True):
        groups.setdefault(_cwd_group(thread), []).append(thread)

    for index, (cwd, group_threads) in enumerate(groups.items()):
        if index:
            items.append(SpacerItem())
        items.append(HeaderItem(_cwd_header_text(cwd), classes="cwd-header"))
        items.extend(ThreadListItem(thread) for thread in group_threads)
    return items


def _cwd_group(thread: CodexThread) -> str:
    if not thread.cwd:
        return "(unknown cwd)"
    return str(Path(thread.cwd).expanduser())


def _cwd_header_text(cwd: str) -> Text:
    path = Path(cwd)
    dirname = path.name or cwd
    text = Text(dirname, style="cyan")
    text.append(f" [{cwd}]", style="bright_black")
    return text


def _help_text() -> Text:
    text = Text()
    shortcuts = (
        ("q", "quit"),
        ("↑/↓", "threads"),
        ("←/→", "projects"),
        ("enter", "delete"),
    )
    for index, (keys, label) in enumerate(shortcuts):
        if index:
            text.append("   ")
        text.append(keys, style="bright_black")
        text.append(f" {label}", style="dim bright_black")
    return text
