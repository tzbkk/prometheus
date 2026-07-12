"""BaseTab — lifecycle + refresh hooks for all TUI tabs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Container

if TYPE_CHECKING:
    from src.tui.api_client import ApiClient


class BaseTab(Container):
    TITLE: str = "Tab"

    def __init__(self, *args, api_client: "ApiClient | None" = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_client = api_client

    def on_mount(self) -> None:
        pass

    def on_show(self) -> None:
        pass

    def refresh_data(self) -> None:
        pass
