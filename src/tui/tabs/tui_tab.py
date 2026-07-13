"""TuiTab — config editor for conf/*.json files, about info, and help/shortcuts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import textual
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
)

from src.prometheus import __version__ as _APP_VERSION
from src.tui.api import PrometheusApiClient
from src.tui.tabs.base import BaseTab

# parents[3]: src/tui/tabs/tui_tab.py → src/tui/tabs → src/tui → src → root
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
_CONF_DIR: Path = _PROJECT_ROOT / "conf"


def _scan_conf_files() -> list[str]:
    if not _CONF_DIR.is_dir():
        return []
    return sorted(f.name for f in _CONF_DIR.glob("*.json") if f.is_file())


def _load_conf_json(file_name: str) -> dict[str, Any]:
    path = _CONF_DIR / file_name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class TuiTab(BaseTab):
    TITLE = "TUI"

    DEFAULT_CSS = """
    TuiTab {
        layout: vertical;
        height: 1fr;
    }

    TuiTab #tui-scroll {
        height: 1fr;
        padding: 0 2;
    }

    TuiTab #config-header {
        padding: 0 0 1 0;
        text-style: bold;
        color: $accent;
    }
    TuiTab #file-selector {
        height: auto;
        margin: 0 0 1 0;
    }
    TuiTab #file-selector-empty {
        padding: 1 0;
        color: $text-disabled;
        text-style: italic;
    }

    TuiTab #config-fields Label {
        padding: 1 0 0 0;
        text-style: bold;
        color: $text-muted;
    }
    TuiTab #config-placeholder {
        padding: 1 0;
        color: $text-disabled;
        text-style: italic;
    }
    TuiTab #config-fields .complex-label {
        padding: 1 0 0 0;
        color: $text-disabled;
        text-style: italic;
    }

    TuiTab #save-config-btn {
        margin: 1 0;
        background: $accent 30%;
    }

    TuiTab .section-header {
        padding: 1 0 0 0;
        text-style: bold;
        color: $accent;
        border-top: solid $accent-darken-1;
    }

    TuiTab #about {
        padding: 0 0 1 0;
    }
    TuiTab #help {
        padding: 0 0 1 0;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.prom_client: PrometheusApiClient | None = None
        self._conf_files: list[str] = _scan_conf_files()
        self._current_file: str | None = None
        self._config_data: dict[str, Any] = {}

    def on_mount(self) -> None:
        if self.api_client is not None:
            self.prom_client = PrometheusApiClient(self.api_client)
        if self._conf_files:
            self.run_worker(self._load_file(self._conf_files[0]))

    def compose(self) -> ComposeResult:
        radio_buttons = [
            RadioButton(fname, id=f"file-{i}", value=(i == 0))
            for i, fname in enumerate(self._conf_files)
        ]

        with VerticalScroll(id="tui-scroll"):
            yield Static("Configuration Editor", id="config-header", markup=False)
            if radio_buttons:
                yield RadioSet(*radio_buttons, id="file-selector")
            else:
                yield Static(
                    "(no .json files found in conf/)",
                    id="file-selector-empty",
                    markup=False,
                )
            with Vertical(id="config-fields"):
                yield Static(
                    "(select a file to edit)",
                    id="config-placeholder",
                    markup=False,
                )
            yield Button(
                "Save Config  [Ctrl+S]",
                id="save-config-btn",
                variant="primary",
            )

            yield Static("About", classes="section-header", markup=False)
            yield Static(self._build_about_text(), id="about", markup=False)

            yield Static(
                "Help / Shortcuts", classes="section-header", markup=False
            )
            yield Static(self._build_help_text(), id="help", markup=False)

    async def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "file-selector":
            return
        pressed_id = event.pressed.id
        if not pressed_id or not pressed_id.startswith("file-"):
            return
        try:
            idx = int(pressed_id[len("file-"):])
        except ValueError:
            return
        if 0 <= idx < len(self._conf_files):
            await self._load_file(self._conf_files[idx])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-config-btn":
            self.action_save_config()

    def on_key(self, event: events.Key) -> None:
        focused = self.app.focused
        if event.key == "ctrl+s":
            event.prevent_default()
            self.action_save_config()
            return
        # Don't swallow keys meant for input widgets.
        if isinstance(focused, (Input, Checkbox)):
            return

    async def _load_file(self, file_name: str) -> None:
        self._current_file = file_name
        try:
            self._config_data = _load_conf_json(file_name)
        except (OSError, json.JSONDecodeError) as exc:
            await self._show_form_error(f"Failed to load {file_name}: {exc}")
            return
        await self._build_config_form()

    async def _build_config_form(self) -> None:
        container = self.query_one("#config-fields", Vertical)
        await container.remove_children()

        new_widgets: list[Any] = []
        for key in sorted(self._config_data.keys()):
            value = self._config_data[key]

            # bool MUST be checked before int (bool subclasses int in Python).
            if isinstance(value, bool):
                new_widgets.append(
                    Checkbox(label=key, value=value, id=f"cfg-{key}")
                )
            elif isinstance(value, int):
                new_widgets.append(Label(key, markup=False))
                new_widgets.append(
                    Input(
                        value=str(value),
                        placeholder=key,
                        id=f"cfg-{key}",
                        type="integer",
                    )
                )
            elif isinstance(value, float):
                new_widgets.append(Label(key, markup=False))
                new_widgets.append(
                    Input(
                        value=str(value),
                        placeholder=key,
                        id=f"cfg-{key}",
                        type="number",
                    )
                )
            elif isinstance(value, str):
                new_widgets.append(Label(key, markup=False))
                new_widgets.append(
                    Input(
                        value=value,
                        placeholder=key,
                        id=f"cfg-{key}",
                        type="text",
                    )
                )
            elif value is None:
                # Skip null values (auto-detect at runtime).
                continue
            else:
                # list / dict — not editable in the simple form.
                new_widgets.append(
                    Label(
                        f"{key}  (complex, edit manually)",
                        classes="complex-label",
                        markup=False,
                    )
                )

        if new_widgets:
            await container.mount_all(new_widgets)
        else:
            await container.mount(
                Static(
                    "(no editable fields in this file)",
                    id="config-placeholder",
                    markup=False,
                )
            )

    async def _show_form_error(self, msg: str) -> None:
        container = self.query_one("#config-fields", Vertical)
        await container.remove_children()
        await container.mount(Static(msg, id="config-placeholder", markup=False))

    def _collect_config(self) -> dict[str, Any]:
        result: dict[str, Any] = {}

        for widget in self.query(Input):
            wid = widget.id
            if not wid or not wid.startswith("cfg-"):
                continue
            key = wid[len("cfg-"):]
            raw = widget.value
            if widget.type == "integer":
                try:
                    result[key] = int(raw)
                except ValueError:
                    result[key] = raw
            elif widget.type == "number":
                try:
                    result[key] = float(raw)
                except ValueError:
                    result[key] = raw
            else:
                result[key] = raw

        for widget in self.query(Checkbox):
            wid = widget.id
            if not wid or not wid.startswith("cfg-"):
                continue
            key = wid[len("cfg-"):]
            result[key] = bool(widget.value)

        return result

    def action_save_config(self) -> None:
        if self._current_file is None:
            self.app.notify("No file selected", severity="warning")
            return

        form_values = self._collect_config()

        if self._current_file == "prometheus.conf.json":
            self._save_via_api(form_values)
        else:
            self._save_to_file(form_values)

    def _save_via_api(self, form_values: dict[str, Any]) -> None:
        if self.prom_client is None:
            self.app.notify(
                "Save failed: API client not available", severity="error"
            )
            return
        try:
            self.prom_client.update_config(form_values)
        except Exception as exc:
            self.app.notify(f"Save failed: {exc}", severity="error")
            return
        self.app.notify("Config saved", severity="information")

    def _save_to_file(self, form_values: dict[str, Any]) -> None:
        # Merge to preserve complex fields (lists/dicts) skipped by the form.
        file_name = self._current_file
        if file_name is None:
            return
        merged: dict[str, Any] = dict(self._config_data)
        merged.update(form_values)
        path = _CONF_DIR / file_name
        try:
            with path.open("w", encoding="utf-8") as fh:
                json.dump(merged, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        except OSError as exc:
            self.app.notify(f"Save failed: {exc}", severity="error")
            return
        self._config_data = merged
        self.app.notify("Config saved", severity="information")

    def _build_about_text(self) -> str:
        api_version = "unknown"
        try:
            tui_conf = _load_conf_json("tui.conf.json")
            api_version = str(tui_conf.get("api_version", "unknown"))
        except (OSError, json.JSONDecodeError):
            pass

        lines = [
            f"prometheus-tui v{_APP_VERSION}",
            f"textual v{textual.__version__}",
            f"API Version: {api_version}",
            f"Project: {_PROJECT_ROOT}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _build_help_text() -> str:
        lines = [
            "Keys:",
            "  Tab       Switch tabs",
            "  S         Start QQ",
            "  K         Kill QQ",
            "  R         Restart QQ",
            "  T         Trigger daemon scan",
            "  Up/Down   Pause/Resume log auto-scroll",
            "  Ctrl+S    Save config",
            "  Q         Quit",
        ]
        return "\n".join(lines)

    def refresh_data(self) -> None:
        pass
