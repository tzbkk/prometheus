"""ViewerTab — viewer process control, archive stats, and config editor."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Static

from src.tui.api import LauncherApiClient
from src.tui.tabs.base import BaseTab

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
_CONF_PATH: Path = _PROJECT_ROOT / "conf" / "viewer.conf.json"


def _load_conf_keys() -> list[str]:
    try:
        with _CONF_PATH.open("r", encoding="utf-8") as fh:
            return list(json.load(fh).keys())
    except Exception:
        return ["port", "db_path", "data_dir", "static_dir", "poll_interval", "page_size"]


class ViewerTab(BaseTab):
    TITLE = "Viewer"
    can_focus = True

    DEFAULT_CSS = """
    ViewerTab {
        padding: 1 2;
    }
    ViewerTab #viewer-status {
        height: 1;
        text-style: bold;
        margin-bottom: 1;
    }
    ViewerTab #viewer-hint {
        height: 1;
        color: $text-muted;
        margin-bottom: 1;
    }
    ViewerTab #viewer-stats {
        padding: 1 0;
    }
    ViewerTab #config-section {
        margin-top: 1;
    }
    ViewerTab .config-row {
        height: 3;
    }
    ViewerTab .config-key {
        width: 16;
        padding: 1 0;
        color: $text-muted;
    }
    ViewerTab .config-val {
        width: 1fr;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.launcher_client: LauncherApiClient | None = None
        self._keys: list[str] = _load_conf_keys()

    def on_mount(self) -> None:
        if self.api_client is not None:
            self.launcher_client = LauncherApiClient(self.api_client)
        self._load_config()

    def on_show(self) -> None:
        self.focus()

    def compose(self) -> ComposeResult:
        yield Static("○ STOPPED", id="viewer-status")
        yield Static(
            "[V] start/stop  [Q]uit",
            id="viewer-hint",
            markup=False,
        )
        yield Static("Feeds: --  |  Media: --  |  DB: --", id="viewer-stats")

        yield Label("Configuration", id="config-section-label")
        with Vertical(id="config-section"):
            for key in self._keys:
                with Horizontal(classes="config-row"):
                    yield Label(key, classes="config-key")
                    yield Input(id=f"cfg-{key}", classes="config-val")
            yield Button("Save Config  [Ctrl+S]", id="save-config-btn", variant="primary")

    def on_key(self, event: events.Key) -> None:
        if event.key == "v":
            event.prevent_default()
            self.action_toggle_viewer()
        elif event.key == "q":
            self.app.action_quit()
        elif event.key == "ctrl+s":
            event.prevent_default()
            self.action_save_config()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-config-btn":
            self.action_save_config()

    def refresh_data(self) -> None:
        if self.launcher_client is None:
            return
        try:
            self._poll_viewer()
        except Exception:
            pass

    def _poll_viewer(self) -> None:
        if self.launcher_client is None:
            return

        state = "stopped"
        try:
            status = self.launcher_client.viewer_status()
            if isinstance(status, dict):
                state = str(status.get("viewer", "stopped")).lower()
        except Exception:
            state = "stopped"

        status_widget = self.query_one("#viewer-status", Static)

        if state == "running":
            status_widget.update("● RUNNING")
        elif state == "crashed":
            status_widget.update("✕ CRASHED")
        else:
            status_widget.update("○ STOPPED")

        if state == "running":
            self._fetch_viewer_stats()
        else:
            self.query_one("#viewer-stats", Static).update(
                "Feeds: --  |  Media: --  |  DB: --"
            )

    def _fetch_viewer_stats(self) -> None:
        try:
            url = "http://127.0.0.1:9422/api/stats"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            feeds = data.get("feed_count", data.get("total_feeds", "--"))
            media = data.get("media_count", data.get("total_media", "--"))
            db_size = data.get("db_size", "--")
            if isinstance(db_size, (int, float)) and db_size > 0:
                db_str = f"{db_size / 1024 / 1024:.0f}MB"
            else:
                db_str = "--"
            self.query_one("#viewer-stats", Static).update(
                f"Feeds: {feeds}  |  Media: {media}  |  DB: {db_str}"
            )
        except Exception:
            pass

    def _load_config(self) -> None:
        try:
            with _CONF_PATH.open("r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            for key in self._keys:
                inp = self.query_one(f"#cfg-{key}", Input)
                val = cfg.get(key)
                if val is not None:
                    inp.value = str(val)
        except Exception:
            pass

    def action_save_config(self) -> None:
        cfg: dict[str, Any] = {}
        for key in self._keys:
            inp = self.query_one(f"#cfg-{key}", Input)
            val = inp.value.strip()
            if key in ("port", "poll_interval", "page_size"):
                try:
                    cfg[key] = int(val)
                except ValueError:
                    cfg[key] = val
            else:
                cfg[key] = val
        try:
            with _CONF_PATH.open("w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        except Exception as exc:
            self.app.notify(f"Save failed: {exc}", severity="error")
            return
        self.app.notify("Viewer config saved", severity="information")

    @work(exclusive=False, thread=True)
    def action_toggle_viewer(self) -> None:
        if self.launcher_client is None:
            return
        try:
            status = self.launcher_client.viewer_status()
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify, f"Viewer status failed: {exc}", severity="error"
            )
            return
        state = (
            status.get("viewer", "stopped") if isinstance(status, dict) else "stopped"
        )
        try:
            if state == "running":
                self.launcher_client.stop_viewer()
                self.app.call_from_thread(
                    self.app.notify, "Viewer stopped", severity="information"
                )
            else:
                self.launcher_client.start_viewer()
                self.app.call_from_thread(
                    self.app.notify,
                    "Viewer started at http://127.0.0.1:9422",
                    severity="information",
                )
        except Exception as exc:
            self.app.call_from_thread(
                self.app.notify, f"Viewer toggle failed: {exc}", severity="error"
            )
