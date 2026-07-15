from __future__ import annotations

import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from src.tui.api import LauncherApiClient, PrometheusApiClient
from src.tui.api_client import ApiClient
from src.tui.tabs.base import BaseTab
from src.tui.tabs.prometheus_tab import PrometheusTab
from src.tui.tabs.tui_tab import TuiTab
from src.tui.tabs.viewer_tab import ViewerTab

# parents[2]: src/tui/app.py → src/tui → src → root
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
_CONF_DIR: Path = _PROJECT_ROOT / "conf"


def _load_tui_api_version() -> str | None:
    """Read expected api_version from conf/tui.conf.json. None if unavailable."""
    try:
        with (_CONF_DIR / "tui.conf.json").open("r", encoding="utf-8") as fh:
            return str(json.load(fh).get("api_version"))
    except (OSError, json.JSONDecodeError):
        return None


class PrometheusApp(App):
    CSS = """
    Screen {
        background: $surface;
        color: $text;
    }
    #connection-banner {
        height: 1;
        background: red;
        color: white;
        text-align: center;
        text-style: bold;
        display: none;
    }
    #connection-banner.connected {
        background: green;
    }
    #connection-banner.warning {
        background: yellow;
        color: black;
    }
    TabbedContent {
        height: 1fr;
    }
    TabPane {
        padding: 1 2;
    }
    #title {
        text-style: bold;
        color: $accent;
        padding: 0 0 1 0;
    }
    #status, #stats, #logs, #about, #help {
        padding: 0 0 1 0;
    }
    """

    TABS: list[type[BaseTab]] = [PrometheusTab, ViewerTab, TuiTab]

    bindings = [
        Binding("q", "quit", "Quit", priority=True),
        ("d", "toggle_dark", "Toggle dark mode"),
    ]

    def __init__(self, launcher_port: int = 9421, poll_interval: int = 2):
        super().__init__()
        self.launcher_port = launcher_port
        self.poll_interval = poll_interval
        self.api_client = ApiClient(launcher_port=launcher_port)
        self._tabs: list[BaseTab] = [
            cls(api_client=self.api_client) for cls in self.TABS
        ]
        self._prom_client = PrometheusApiClient(self.api_client)
        self._launcher_client = LauncherApiClient(self.api_client)
        self._expected_api_version: str | None = _load_tui_api_version()
        self._was_disconnected: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="connection-banner")
        with TabbedContent():
            for tab in self._tabs:
                pane_id = f"tab-{tab.TITLE.lower()}"
                yield TabPane(tab.TITLE, tab, id=pane_id)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Prometheus"
        self.sub_title = f"launcher :{self.launcher_port}"
        self.set_interval(self.poll_interval, self._refresh_all)

    def _refresh_all(self) -> None:
        try:
            self._update_connection_banner()
        except Exception:
            pass
        for tab in self._tabs:
            try:
                tab.refresh_data()
            except Exception:
                pass

    def _update_connection_banner(self) -> None:
        banner = self.query_one("#connection-banner", Static)

        qq_state = "unknown"
        try:
            status = self._launcher_client.get_status()
            if isinstance(status, dict):
                qq_state = str(status.get("qq", "unknown")).lower()
        except Exception:
            pass

        # Crashed state from launcher takes priority over API health
        if qq_state == "crashed":
            banner.remove_class("connected", "warning")
            banner.update("✕ CRASHED — restart with [R]")
            banner.display = True
            self._was_disconnected = True
            return

        is_healthy = self._prom_client.is_healthy()

        if not is_healthy:
            banner.remove_class("connected", "warning")
            banner.update("DISCONNECTED — Retrying...")
            banner.display = True
            self._was_disconnected = True
            return

        mismatch = self._check_api_version()
        if mismatch is not None:
            expected, got = mismatch
            banner.remove_class("connected")
            banner.add_class("warning")
            banner.update(
                f"⚠ API version mismatch (expected {expected}, got {got})"
            )
            banner.display = True
            self._was_disconnected = False
            return

        if self._was_disconnected:
            banner.remove_class("warning")
            banner.add_class("connected")
            banner.update("CONNECTED")
            banner.display = True
            self._was_disconnected = False
            self.set_timer(2.0, self._hide_connected_banner)
        else:
            banner.display = False

    def _check_api_version(self) -> tuple[str, str] | None:
        if self._expected_api_version is None:
            return None
        try:
            config = self.api_client.get_config()
        except Exception:
            return None
        if not isinstance(config, dict):
            return None
        remote_version = str(config.get("apiVersion", "unknown"))
        if remote_version != self._expected_api_version:
            return (self._expected_api_version, remote_version)
        return None

    def _hide_connected_banner(self) -> None:
        # Guard: don't hide if banner changed to error/mismatch during the 2s delay
        try:
            banner = self.query_one("#connection-banner", Static)
        except Exception:
            return
        if "connected" in banner.classes:
            banner.display = False

    def action_toggle_dark(self) -> None:
        self.theme = (
            "textual-dark" if self.theme != "textual-dark" else "textual-light"
        )

    def on_key(self, event: events.Key) -> None:
        if event.key == "q":
            event.stop()
            self.exit()

    def action_quit(self) -> None:
        self.exit()
