"""PrometheusTab — QQ process control, stats, config editor and live log viewer."""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual import events
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    RichLog,
    Select,
    Static,
)

from src.tui.api import LauncherApiClient, PrometheusApiClient
from src.tui.tabs.base import BaseTab

_LEVEL_COLORS: dict[str, str] = {
    "ERROR": "red",
    "WARN": "yellow",
    "INFO": "white",
    "DEBUG": "dim grey",
}

_LEVEL_OPTIONS: tuple[str, ...] = ("ALL", "ERROR", "WARN", "INFO", "DEBUG")

# Values are arrays/objects — not editable in the simple form editor.
_COMPLEX_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "startup_sequence",
        "media_subdirs",
        "monitor_targets",
        "channel_ids",
        "feed_ids",
        "targets",
        "guilds",
    }
)

# Per-complex-key human-readable notes shown in the config editor. Keys
# absent from this map fall back to the generic "complex, edit manually" note.
_COMPLEX_CONFIG_NOTES: dict[str, str] = {
    "guilds": "guilds  (edit manually in conf/guilds.conf.json)",
}

# QQ API field-name fallbacks: the exact keys may vary, so try common aliases.
_LOG_LIST_KEYS = ("logs", "lines")
_LOG_SEQ_KEYS = ("seq", "sequence", "id")
_LOG_LEVEL_KEYS = ("level", "lvl", "severity")
_LOG_MSG_KEYS = ("msg", "message", "text", "line")
_LOG_TS_KEYS = ("ts", "timestamp", "time", "date", "iso")
_STAT_FEED_KEYS = ("feeds", "feed_count", "total_feeds", "feeds_count")
_STAT_COMMENT_KEYS = ("comments", "comment_count", "total_comments", "comments_count")
_STAT_MEDIA_KEYS = ("media_total", "media", "media_files", "media_count", "total_media")
_STAT_DEAD_KEYS = ("dead", "dead_links", "dead_media", "dead_count")
_DAEMON_INTERVAL_KEYS = ("daemonInterval", "daemon_interval_ms", "daemon_interval", "interval_ms", "scraper_daemon_interval_sec")
_DAEMON_LAST_KEYS = ("last_scan_ts", "last_daemon_ts", "last_scan", "last_daemon")


def _first(d: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    """Return the first present value among *keys* in *d*."""
    for k in keys:
        if k in d:
            return d[k]
    return default


class PrometheusTab(BaseTab):
    TITLE = "Prometheus"

    DEFAULT_CSS = """
    PrometheusTab {
        layout: horizontal;
        height: 1fr;
        width: 1fr;
    }

    PrometheusTab #left-panel {
        width: 2fr;
        height: 1fr;
        padding: 0 1 0 0;
    }
    PrometheusTab #right-panel {
        width: 3fr;
        height: 1fr;
    }

    PrometheusTab #left-panel {
        layout: vertical;
    }
    PrometheusTab #process-status {
        padding: 1 2;
        background: $panel;
        border: solid $accent-darken-1;
        text-style: bold;
    }
    PrometheusTab #controls-hint {
        padding: 1 2;
        color: $text-muted;
        text-style: italic;
    }
    PrometheusTab #stats-display {
        padding: 0 2 1 2;
    }
    PrometheusTab #guilds-display {
        padding: 0 2 1 2;
        color: $text-muted;
    }
    PrometheusTab #daemon-countdown {
        padding: 0 2 1 2;
        color: $accent;
        text-style: bold;
    }
    PrometheusTab #config-section-label {
        padding: 1 2 0 2;
        text-style: bold;
        border-top: solid $accent-darken-1;
    }
    PrometheusTab #config-scroll {
        height: 1fr;
        padding: 0 2;
    }
    PrometheusTab #config-scroll Label {
        padding: 1 0 0 0;
        text-style: bold;
        color: $text-muted;
    }
    PrometheusTab #config-scroll Input {
        margin: 0 0 0 0;
    }
    PrometheusTab #config-placeholder {
        padding: 1 0;
        color: $text-disabled;
        text-style: italic;
    }
    PrometheusTab #save-config-btn {
        margin: 1 0;
        background: $accent 30%;
    }

    PrometheusTab #right-panel {
        layout: vertical;
    }
    PrometheusTab #filter-bar {
        height: 3;
        padding: 0 0 1 0;
    }
    PrometheusTab #filter-bar Input {
        width: 1fr;
    }
    PrometheusTab #filter-bar Select {
        width: 16;
        margin: 0 0 0 1;
    }
    PrometheusTab #log-viewer {
        height: 1fr;
        border: solid $accent-darken-1;
        background: $surface-darken-1;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.prom_client: PrometheusApiClient | None = None
        self.launcher_client: LauncherApiClient | None = None
        self._last_log_seq: int = 0
        self._last_qq_pid: int | None = None
        self._last_scraper_pid: int | None = None
        self._config_loaded: bool = False
        self._auto_scroll_paused: bool = False
        self._last_daemon_time: float | None = None
        self._qq_uptime: int | None = None
        self._daemon_interval_s: float | None = None

    def on_mount(self) -> None:
        if self.api_client is not None:
            self.prom_client = PrometheusApiClient(self.api_client)
            self.launcher_client = LauncherApiClient(self.api_client)

    def compose(self) -> ComposeResult:
        with Vertical(id="left-panel"):
            yield Static("○ DISCONNECTED", id="process-status")
            yield Static(
                "[S]tart  [K]ill  [R]estart  [T]rigger  [Shift+S]craper on/off  [Q]uit",
                id="controls-hint",
                markup=False,
            )
            yield Static(
                "Feeds: -- | Comments: -- | Media: -- | Dead: --",
                id="stats-display",
            )
            yield Static("", id="guilds-display", markup=False)
            yield Static("Next scan: ---", id="daemon-countdown")
            yield Static("Configuration", id="config-section-label")
            with VerticalScroll(id="config-scroll"):
                with Vertical(id="config-fields"):
                    yield Static(
                        "(waiting for config...)",
                        id="config-placeholder",
                    )
                yield Button(
                    "Save Config  [Ctrl+S]",
                    id="save-config-btn",
                    variant="primary",
                )

        with Vertical(id="right-panel"):
            with Horizontal(id="filter-bar"):
                yield Input(
                    placeholder="Filter (substring, case-insensitive)...",
                    id="log-filter",
                )
                yield Select(
                    [(lvl, lvl) for lvl in _LEVEL_OPTIONS],
                    value="ALL",
                    id="level-filter",
                )
            yield RichLog(
                id="log-viewer",
                markup=False,
                auto_scroll=True,
                wrap=True,
                min_width=60,
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-config-btn":
            self.action_save_config()

    def on_key(self, event: events.Key) -> None:
        """s/k/r/t fire control actions, Shift+S toggles scraper, Ctrl+S saves config.

        Action keys are suppressed when an Input/Select/Checkbox has focus so
        the user can type freely; Ctrl+S works from anywhere.
        """
        focused = self.app.focused
        key = event.key

        if key == "ctrl+s":
            event.prevent_default()
            self.action_save_config()
            return

        if key == "q":
            self.app.action_quit()
            return

        if isinstance(focused, (Input, Select, Checkbox)):
            if key == "q":
                self.app.action_quit()
            return

        if key in ("S", "shift+s"):
            event.prevent_default()
            self.action_toggle_scraper()
            return

        if key == "s":
            event.prevent_default()
            self.action_start_qq()
        elif key == "k":
            event.prevent_default()
            self.action_kill_qq()
        elif key == "r":
            event.prevent_default()
            self.action_restart_qq()
        elif key == "t":
            event.prevent_default()
            self.action_trigger_daemon()
        elif key == "up":
            event.prevent_default()
            self._pause_auto_scroll()
        elif key == "down":
            event.prevent_default()
            self._resume_auto_scroll()

    def refresh_data(self) -> None:
        if self.prom_client is None or self.launcher_client is None:
            return
        self._refresh_dashboard()
        self._refresh_launcher_status()
        self._refresh_logs()
        self._refresh_countdown()

    def _refresh_dashboard(self) -> None:
        assert self.prom_client is not None
        try:
            data = self.prom_client.get_dashboard_data()
        except Exception:
            self._set_disconnected()
            return

        if "error" in data and not any(
            k in data for k in ("stats", "logs", "config")
        ):
            self._set_disconnected()

        stats = data.get("stats")
        if isinstance(stats, dict):
            self._update_stats(stats)
            self._update_guilds_stats(stats)
            scan_ts = _first(stats, _DAEMON_LAST_KEYS)
            if isinstance(scan_ts, (int, float)) and scan_ts > 0:
                self._last_daemon_time = float(scan_ts)
            elif self._last_daemon_time is None:
                self._last_daemon_time = time.time()
            self._qq_uptime = stats.get("uptime_seconds")

            # daemon_running is the scraper-only field that distinguishes it from QQ stats.
            if "daemon_running" in stats:
                self._set_scraper_status(stats)

        config = data.get("config")
        if isinstance(config, dict):
            if not self._config_loaded:
                self._build_config_editor(config)
                self._config_loaded = True
            for _ik in _DAEMON_INTERVAL_KEYS:
                if _ik in config and isinstance(config[_ik], (int, float)) and config[_ik] > 0:
                    raw = float(config[_ik])
                    self._daemon_interval_s = raw if "sec" in _ik else raw / 1000.0
                    break

    def _refresh_launcher_status(self) -> None:
        assert self.launcher_client is not None
        try:
            status = self.launcher_client.get_status()
        except Exception:
            return
        if isinstance(status, dict):
            self._update_status(status)

    def _refresh_logs(self) -> None:
        assert self.api_client is not None
        try:
            logs_data = self.api_client.get_logs(
                since=self._last_log_seq, max_lines=200
            )
        except Exception:
            return
        if isinstance(logs_data, dict):
            self._append_logs(logs_data)

    def _refresh_countdown(self) -> None:
        widget = self.query_one("#daemon-countdown", Static)
        if (
            self._daemon_interval_s is None
            or self._daemon_interval_s <= 0
            or self._last_daemon_time is None
        ):
            widget.update("Next scan: ---")
            return
        elapsed = time.time() - self._last_daemon_time
        while elapsed >= self._daemon_interval_s:
            self._last_daemon_time += self._daemon_interval_s
            elapsed = time.time() - self._last_daemon_time
        remaining = self._daemon_interval_s - elapsed
        widget.update(f"Next scan: {int(remaining)}s")

    def _update_status(self, status: dict) -> None:
        widget = self.query_one("#process-status", Static)

        scraper_pid = status.get("scraper_pid")
        if (
            scraper_pid is not None
            and self._last_scraper_pid is not None
            and scraper_pid != self._last_scraper_pid
        ):
            self._last_log_seq = 0
        if scraper_pid is not None:
            self._last_scraper_pid = scraper_pid

        scraper_state = str(status.get("scraper", "stopped")).lower()
        if scraper_state == "running":
            self._render_scraper_launcher_status(status)
            return

        qq_state = str(status.get("qq", "unknown")).lower()

        pid = status.get("qq_pid")
        if pid is not None and self._last_qq_pid is not None and pid != self._last_qq_pid:
            self._last_log_seq = 0
        if pid is not None:
            self._last_qq_pid = pid

        if qq_state == "running":
            marker, color = "● RUNNING", "green"
        elif qq_state == "stopped":
            marker, color = "○ STOPPED", "red"
        elif qq_state == "crashed":
            marker, color = "✕ CRASHED", "red"
        else:
            marker, color = "? UNKNOWN", "yellow"

        pid = status.get("qq_pid", "??")
        uptime = self._qq_uptime
        if uptime is not None:
            uptime_str = f"{int(uptime)}s"
        else:
            uptime_str = "--"
        widget.update(f"[{color}]{marker}[/]  PID:{pid}  up:{uptime_str}")

    def _render_scraper_launcher_status(self, status: dict) -> None:
        widget = self.query_one("#process-status", Static)
        marker, color = "● SCRAPER RUNNING", "green"
        scanned = self._scraped_cycles if hasattr(self, "_scraped_cycles") else "--"
        widget.update(f"[{color}]{marker}[/]  (launcher-managed)")

    def _set_scraper_status(self, stats: dict) -> None:
        widget = self.query_one("#process-status", Static)
        if bool(stats.get("daemon_running", False)):
            marker, color = "● SCRAPER RUNNING", "green"
        else:
            marker, color = "○ SCRAPER IDLE", "yellow"
        scanned = stats.get("scanned_feeds", "--")
        widget.update(f"[{color}]{marker}[/]  scanned:{scanned}")

    def _update_stats(self, stats: dict) -> None:
        widget = self.query_one("#stats-display", Static)
        feeds = _first(stats, _STAT_FEED_KEYS, "--")
        comments = _first(stats, _STAT_COMMENT_KEYS, "--")
        media = _first(stats, _STAT_MEDIA_KEYS, "--")
        dead = _first(stats, _STAT_DEAD_KEYS, "--")
        widget.update(
            f"Feeds: {feeds} | Comments: {comments} | Media: {media} | Dead: {dead}"
        )

        interval_raw = _first(stats, _DAEMON_INTERVAL_KEYS)
        if isinstance(interval_raw, (int, float)) and interval_raw > 0:
            self._daemon_interval_s = float(interval_raw) if interval_raw < 1000 else float(interval_raw) / 1000.0

        last_scan = _first(stats, _DAEMON_LAST_KEYS)
        if last_scan is not None:
            try:
                self._last_daemon_time = float(last_scan)
            except (TypeError, ValueError):
                pass

    def _update_guilds_stats(self, stats: dict) -> None:
        """Render the per-guild breakdown under TOTALS.

        Defensive against older scrapers that omit ``stats["guilds"]``: the
        widget is cleared and stays blank so the TOTALS line above keeps
        working unchanged (backward-compat §7.3).
        """
        widget = self.query_one("#guilds-display", Static)
        guilds_data = stats.get("guilds", {})
        if not isinstance(guilds_data, dict) or not guilds_data:
            widget.update("")
            return

        config_block = stats.get("config", {})
        config_guilds = (
            config_block.get("guilds", []) if isinstance(config_block, dict) else []
        )
        guild_names: dict[str, str] = {}
        if isinstance(config_guilds, list):
            for g in config_guilds:
                if isinstance(g, dict) and g.get("guild_id"):
                    guild_names[str(g["guild_id"])] = g.get("name") or str(g["guild_id"])

        def _feed_count(item: tuple[str, Any]) -> int:
            _, g = item
            if not isinstance(g, dict):
                return 0
            try:
                return int(_first(g, _STAT_FEED_KEYS, 0))
            except (TypeError, ValueError):
                return 0

        lines: list[str] = []
        for gid, g in sorted(guilds_data.items(), key=_feed_count, reverse=True):
            if not isinstance(g, dict):
                continue
            name = guild_names.get(str(gid), str(gid))
            feeds = _first(g, _STAT_FEED_KEYS, "--")
            comments = _first(g, _STAT_COMMENT_KEYS, "--")
            media = _first(g, _STAT_MEDIA_KEYS, "--")
            lines.append(
                f"  {name} ({gid}): {feeds} feeds | {comments} comments | {media} media"
            )
        widget.update("\n".join(lines))

    def _set_disconnected(self) -> None:
        widget = self.query_one("#process-status", Static)
        widget.update("[red]✕ DISCONNECTED[/]")

    def _build_config_editor(self, config: dict) -> None:
        fields_container = self.query_one("#config-fields", Vertical)
        try:
            self.query_one("#config-placeholder", Static).remove()
        except Exception:
            pass

        new_widgets: list[Any] = []
        for key in sorted(config.keys()):
            if key in _COMPLEX_CONFIG_KEYS:
                note = _COMPLEX_CONFIG_NOTES.get(
                    key, f"{key}  (complex, edit manually)"
                )
                new_widgets.append(Label(note, markup=False))
                continue
            value = config[key]
            field_id = f"config-{key}"

            # bool MUST be checked before int (bool subclasses int in Python).
            if isinstance(value, bool):
                new_widgets.append(
                    Checkbox(label=key, value=value, id=field_id)
                )
            elif isinstance(value, int):
                new_widgets.append(Label(key))
                new_widgets.append(
                    Input(
                        value=str(value),
                        placeholder=key,
                        id=field_id,
                        type="integer",
                    )
                )
            elif isinstance(value, float):
                new_widgets.append(Label(key))
                new_widgets.append(
                    Input(
                        value=str(value),
                        placeholder=key,
                        id=field_id,
                        type="number",
                    )
                )
            elif isinstance(value, str):
                new_widgets.append(Label(key))
                new_widgets.append(
                    Input(value=value, placeholder=key, id=field_id, type="text")
                )

        if new_widgets:
            fields_container.mount_all(new_widgets)

    def _collect_config(self) -> dict:
        result: dict[str, Any] = {}

        for widget in self.query(Input):
            wid = widget.id
            if not wid or not wid.startswith("config-"):
                continue
            key = wid[len("config-"):]
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
            if not wid or not wid.startswith("config-"):
                continue
            key = wid[len("config-"):]
            result[key] = bool(widget.value)

        return result

    def action_save_config(self) -> None:
        if self.prom_client is None:
            return
        new_config = self._collect_config()
        try:
            self.prom_client.update_config(new_config)
        except Exception as exc:
            self.app.notify(f"Config save failed: {exc}", severity="error")
            return
        self.app.notify("Config saved", severity="information")

    @work(exclusive=False, thread=True)
    def action_start_qq(self) -> None:
        if self.launcher_client is None:
            return
        try:
            self.launcher_client.start_qq()
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"Start failed: {exc}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, "Start signal sent", severity="information")

    @work(exclusive=False, thread=True)
    def action_kill_qq(self) -> None:
        if self.launcher_client is None:
            return
        try:
            self.launcher_client.stop_qq()
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"Stop failed: {exc}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, "Stop signal sent", severity="information")

    @work(exclusive=False, thread=True)
    def action_restart_qq(self) -> None:
        if self.launcher_client is None:
            return
        self.app.call_from_thread(self.app.notify, "Restarting (may block ~30s)...", severity="information")
        try:
            result = self.launcher_client.restart_qq()
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"Restart failed: {exc}", severity="error")
            return
        if isinstance(result, dict) and result.get("ok", True) is False:
            self.app.call_from_thread(self.app.notify,
                f"Restart failed: {result.get('error', 'unknown')}",
                severity="error",
            )
            return
        self.app.call_from_thread(self.app.notify, "Restarted", severity="information")

    @work(exclusive=False, thread=True)
    def action_trigger_daemon(self) -> None:
        if self.prom_client is None:
            return
        try:
            self.prom_client.trigger_daemon()
        except Exception as exc:
            self.app.notify(f"Trigger failed: {exc}", severity="error")
            return
        self.app.notify("Daemon triggered", severity="information")

    @work(exclusive=False, thread=True)
    def action_toggle_scraper(self) -> None:
        if self.launcher_client is None:
            return
        try:
            status = self.launcher_client.get_status()
        except Exception as exc:
            self.app.notify(f"Scraper status failed: {exc}", severity="error")
            return
        running = status.get("scraper") == "running" if isinstance(status, dict) else False
        try:
            if running:
                self.launcher_client.stop_scraper()
                msg = "Scraper stop signal sent"
            else:
                self.launcher_client.start_scraper()
                msg = "Scraper start signal sent"
        except Exception as exc:
            self.app.notify(f"Scraper toggle failed: {exc}", severity="error")
            return
        self.app.notify(msg, severity="information")

    def _append_logs(self, logs_data: dict) -> None:
        lines = _first(logs_data, _LOG_LIST_KEYS)
        if not isinstance(lines, list) or not lines:
            return

        log_viewer = self.query_one("#log-viewer", RichLog)
        keyword = self._get_keyword_filter().lower()
        level_filter = self._get_level_filter()

        max_seq = self._last_log_seq
        for entry in lines:
            if isinstance(entry, str):
                if keyword and keyword not in entry.lower():
                    continue
                log_viewer.write(entry)
                continue

            if not isinstance(entry, dict):
                continue

            seq = _first(entry, _LOG_SEQ_KEYS)
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq

            level = str(_first(entry, _LOG_LEVEL_KEYS, "INFO")).upper()
            msg = str(_first(entry, _LOG_MSG_KEYS, ""))
            ts_raw = _first(entry, _LOG_TS_KEYS, "")
            ts_str = self._format_ts(ts_raw)

            if level_filter != "ALL" and level != level_filter:
                continue
            if keyword and keyword not in f"{ts_str} {level} {msg}".lower():
                continue

            color = _LEVEL_COLORS.get(level, "white")
            line = Text.assemble(
                (f"{ts_str} ", "dim"),
                (f"[{level}] ".ljust(8), color),
                (msg, color),
            )
            log_viewer.write(line)

        self._last_log_seq = max_seq

    @staticmethod
    def _format_ts(ts_raw: Any) -> str:
        if ts_raw in ("", None):
            return ""
        ts_str = str(ts_raw)
        if "T" in ts_str:
            return ts_str.split("T", 1)[1].split(".", 1)[0]
        return ts_str

    def _get_keyword_filter(self) -> str:
        try:
            return self.query_one("#log-filter", Input).value
        except Exception:
            return ""

    def _get_level_filter(self) -> str:
        try:
            val = self.query_one("#level-filter", Select).value
        except Exception:
            return "ALL"
        return "ALL" if val == Select.NULL else str(val)

    def _pause_auto_scroll(self) -> None:
        log_viewer = self.query_one("#log-viewer", RichLog)
        log_viewer.auto_scroll = False
        self._auto_scroll_paused = True
        log_viewer.border_title = "PAUSED — [↓] resume"

    def _resume_auto_scroll(self) -> None:
        log_viewer = self.query_one("#log-viewer", RichLog)
        log_viewer.auto_scroll = True
        self._auto_scroll_paused = False
        log_viewer.border_title = ""
        log_viewer.scroll_end(animate=False)
