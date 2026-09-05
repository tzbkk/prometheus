"""Command parser and dispatcher for the launcher shell.

Pure logic layer (no prompt_toolkit). Two components:

1. CommandParser — tokenize user input into a ``Cmd`` dataclass.
2. Dispatcher — execute a ``Cmd`` against a ``ProcessManager``, returning a
   response envelope ``{"ok": bool, "message": str, "data": dict}``.

监督目标 = 三枚举（scraper/deepbackfill/viewer——组件图外的名字解析即拒）。
archive 动词 = 同步直调打包
引擎的批处理入口——不是监督目标，窗口校验归引擎单一执法源。

The Dispatcher is thread-safe: all ``ProcessManager`` calls are serialized via
the ProcessManager's internal lock (REPL thread + monitor thread 共用).
"""

import json
import tempfile
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HELP_TEXT = """Available commands:
  start <scraper|deepbackfill|viewer>    Start a process (deepbackfill: auto QR login if credentials missing)
  stop <scraper|deepbackfill|viewer>     Stop a process
  restart <scraper|deepbackfill|viewer>  Restart a process
  logs <scraper|deepbackfill|viewer>     Tail process log (Ctrl+C to stop)
  auth                    Web QR login for deepbackfill (service must be running)
  archive <guild> <from> <to> [--apply] [--force] [--output DIR]
                          Package a time window (bare archive lists data spans; dry-run default; UTC YYYYMMDD)
  config show             Grouped view: launcher / scraper / viewer / guilds / credentials
  config set <key> <val>  Set a registered key — routed to its owner file
                          (unknown key lists valid keys)
  health [target]         Supervision state + API probe per target (bare = all three)
  stats                   Per-guild counts with month/day availability (entity tree)
  help                    Show this help
  quit                    Close this shell (targets keep running — daemon supervises)
  shutdown                Stop daemon and all targets (full tree down)
  clear                   Clear the screen"""

_ARCHIVE_DATA_ROOT = Path("data")

# config 路由注册表（方案 B）：一键一主——set 路由到属主文件，
# show 分组呈现。viewer 的 port 归 launcher（spawn --port 覆盖）；
# viewer.data_dir/static_dir 与 scraper 撞名不注册（部署路径，少动）。
CONFIG_ROUTES = {
    "launcher_port":               ("launcher", "daemon restart"),
    "max_restarts":                ("launcher", "live"),
    "restart_delay":               ("launcher", "supervisor restart"),
    "viewer_port":                 ("launcher", "next viewer start"),
    "deepbackfill_port":           ("launcher", "next deepbackfill start"),
    "channel_id":                  ("scraper", "restart scraper"),
    "channel_name":                ("scraper", "restart scraper"),
    "guild_number":                ("scraper", "restart scraper"),
    "scraper_max_workers":         ("scraper", "restart scraper"),
    "scraper_daemon_interval_sec": ("scraper", "restart scraper"),
    "scraper_api_port":            ("scraper", "restart scraper"),
    "data_dir":                    ("scraper", "restart scraper"),
    "db_path":                     ("viewer", "restart viewer"),
    "poll_interval":               ("viewer", "restart viewer"),
    "page_size":                   ("viewer", "restart viewer"),
}
_LAUNCHER_KEY_DEFAULTS = {
    "launcher_port": 9421, "max_restarts": 5, "restart_delay": 5,
    "viewer_port": 9422, "deepbackfill_port": 9424,
}
_SCRAPER_KEYS = (
    "channel_id", "channel_name", "guild_number", "scraper_max_workers",
    "scraper_daemon_interval_sec", "scraper_api_port", "data_dir",
)
_VIEWER_KEYS = ("db_path", "poll_interval", "page_size")
_ARCHIVE_OUTPUT_ROOT = Path("archives")


@dataclass
class Cmd:
    """A parsed command: verb (action), noun (target/subcommand), args."""

    verb: str
    noun: str = ""
    args: list = field(default_factory=list)


class UnknownCommandError(Exception):
    """Raised when the verb is not recognised."""


class MissingArgumentError(Exception):
    """Raised when a verb requires a noun/subcommand but none was given."""


class InvalidTargetError(Exception):
    """Raised when a noun is not a valid target."""


class CommandParser:
    """Tokenize user input strings into ``Cmd`` objects.

    Grammar::

        verb:    start | stop | restart | logs | config | health |
                 tail | archive | auth | help | quit | clear
        target:  scraper | deepbackfill | viewer
        config:  config show | config set <key> <value...>
        stats:   stats (no arguments — per-guild archive counts)
        logs:    logs <target>
        archive: archive <guild> <from> <to> [--apply] [--force] [--output DIR]
        auth:    auth (no arguments — interactive flow owned by the Shell)
        health:  health [target] (bare = all three targets)
    """

    VALID_VERBS = {
        "start", "stop", "restart", "logs",
        "config", "health", "stats", "archive", "auth", "help", "quit", "clear",
        "shutdown",
    }
    VALID_TARGETS = {"scraper", "deepbackfill", "viewer"}
    VERBS_REQUIRING_TARGET = {"start", "stop", "restart", "logs"}
    _ARCHIVE_OPTIONS = ("--apply", "--force", "--output")

    def parse(self, text):
        """Parse ``text`` into a :class:`Cmd`.

        Raises :class:`UnknownCommandError`, :class:`MissingArgumentError`,
        or :class:`InvalidTargetError` on invalid input.
        """
        text = text.strip().lower()
        if not text:
            return Cmd(verb="noop")

        tokens = text.split()
        verb = tokens[0]

        if verb not in self.VALID_VERBS:
            raise UnknownCommandError(
                "Unknown command: '{0}'. Type 'help' for available commands.".format(verb)
            )

        if verb in self.VERBS_REQUIRING_TARGET:
            return self._parse_targeted(verb, tokens)

        if verb == "config":
            return self._parse_config(tokens)

        if verb == "stats":
            if len(tokens) > 1:
                raise MissingArgumentError("'stats' takes no arguments.")
            return Cmd(verb="stats")

        if verb == "archive":
            return self._parse_archive(tokens)

        if verb == "auth":
            if len(tokens) > 1:
                raise InvalidTargetError(
                    "'auth' takes no arguments — the web QR login flow is interactive."
                )
            return Cmd(verb="auth")

        if verb == "health":
            if len(tokens) > 2:
                raise MissingArgumentError(
                    "'health' takes at most one target: scraper, deepbackfill,"
                    " or viewer."
                )
            if len(tokens) == 2 and tokens[1] not in self.VALID_TARGETS:
                raise InvalidTargetError(
                    "Invalid target: '{0}'. Must be one of:"
                    " scraper, deepbackfill, viewer.".format(tokens[1])
                )
            return Cmd(verb="health", noun=tokens[1] if len(tokens) == 2 else None)

        return Cmd(verb=verb)

    def _parse_targeted(self, verb, tokens):
        if len(tokens) < 2:
            raise MissingArgumentError(
                "'{0}' requires a target: scraper, deepbackfill, or viewer.".format(verb)
            )
        noun = tokens[1]
        if noun not in self.VALID_TARGETS:
            raise InvalidTargetError(
                "Invalid target: '{0}'. Must be one of: scraper, deepbackfill, viewer.".format(noun)
            )
        return Cmd(verb=verb, noun=noun, args=tokens[2:])

    def _parse_config(self, tokens):
        if len(tokens) < 2:
            raise MissingArgumentError(
                "'config' requires a subcommand: show or set."
            )
        sub = tokens[1]
        if sub == "show":
            return Cmd(verb="config", noun="show", args=[])
        if sub == "set":
            if len(tokens) < 4:
                raise MissingArgumentError(
                    "'config set' requires <key> <value>."
                )
            key = tokens[2]
            value = " ".join(tokens[3:])
            return Cmd(verb="config", noun="set", args=[key, value])
        raise MissingArgumentError(
            "'config' requires a subcommand: show or set."
        )

    def _parse_archive(self, tokens):
        """archive <guild> <from> <to> [--apply] [--force] [--output DIR]。

        窗参格式/日历/倒序校验不做在解析层——引擎是单一执法源
        （WindowError 的消息即 shell 错误面）。
        """
        if len(tokens) == 1:
            return Cmd(verb="archive", noun=None,
                       args=[None, None,
                             {"apply": False, "force": False, "output": None}])
        if len(tokens) == 2:
            return Cmd(verb="archive", noun=tokens[1],
                       args=[None, None,
                             {"apply": False, "force": False, "output": None}])
        if len(tokens) < 4:
            raise MissingArgumentError(
                "'archive' requires <guild> <from> <to>"
                " [--apply] [--force] [--output DIR]."
            )
        guild, from_ymd, to_ymd = tokens[1], tokens[2], tokens[3]
        opts = {"apply": False, "force": False, "output": None}
        rest = tokens[4:]
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--apply":
                opts["apply"] = True
            elif token == "--force":
                opts["force"] = True
            elif token == "--output":
                if i + 1 >= len(rest):
                    raise MissingArgumentError(
                        "'archive --output' requires a directory path."
                    )
                opts["output"] = rest[i + 1]
                i += 1
            else:
                raise InvalidTargetError(
                    "Unknown archive option: '{0}'. Options are: {1}.".format(
                        token, ", ".join(self._ARCHIVE_OPTIONS)
                    )
                )
            i += 1
        return Cmd(verb="archive", noun=guild, args=[from_ymd, to_ymd, opts])


def _iter_guild_dirs(data_root):
    """data_root 下数字 guild 目录（含 feeds/ 或 comments/ 子树）迭代。"""
    try:
        entries = sorted(os.listdir(data_root))
    except OSError:
        return
    for name in entries:
        root = os.path.join(data_root, name)
        if name.isdigit() and (
            os.path.isdir(os.path.join(root, "feeds"))
            or os.path.isdir(os.path.join(root, "comments"))
        ):
            yield root, name


def _entity_create_time(path):
    """实体 createTime（QQ 十进制秒串）→ int 秒；不可读/畸形 → None。

    信息面容忍（全景缺一格不崩）；engine 侧对账链依旧严格
    （resolve → load_entity → createTime fail loud）。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh).get("createTime")
        return int(str(raw))
    except (OSError, ValueError, TypeError):
        return None


def _see_entity(entry, span, path, kind):
    """以 path 实体的 createTime 拓宽 (earliest, latest) 秒对并入逐月直方图。

    月桶 = {feeds/comments/replies 计数, days 集合}——回答「哪些
    YYYYMMDD 可以当窗参」：span 只是模糊范围，月桶+days 才是确切
    可用日（空洞月/空洞日一目了然）。
    """
    ts = _entity_create_time(path)
    if ts is None:
        return span
    lo, hi = span
    if lo is None or ts < lo:
        lo = ts
    if hi is None or ts > hi:
        hi = ts
    t = datetime.fromtimestamp(ts, tz=timezone.utc)
    month = entry["months"].setdefault(
        t.strftime("%Y%m"),
        {"feeds": 0, "comments": 0, "replies": 0, "days": set()})
    month[kind] += 1
    month["days"].add(t.day)
    return (lo, hi)


def _ymd_utc(ts):
    """int 秒 → UTC YYYYMMDD——与 archive 窗参同格式，可直接粘贴回命令。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _day_ranges(days):
    """排序日 → 区间编码：[1,2,3,7,20,21] → "01-03 07 20-21"。"""
    spans = []
    start = prev = None
    for d in days:
        if start is None:
            start = prev = d
        elif d == prev + 1:
            prev = d
        else:
            spans.append((start, prev))
            start = prev = d
    if start is not None:
        spans.append((start, prev))
    return " ".join(
        "{0:02d}".format(a) if a == b else "{0:02d}-{1:02d}".format(a, b)
        for a, b in spans)


def _month_line(ym, m):
    """逐月可用性行：计数 + 确切有数据的日子（可直接拼 YYYYMMDD 窗参）。"""
    return "  {0}  {1} feeds · {2} comments · {3} replies · days {4}".format(
        ym, m["feeds"], m["comments"], m["replies"],
        _day_ranges(sorted(m["days"])) or "-")


def _guild_stat_line(guild, e):
    """一行 guild 全景：计数 + 体积 + createTime 跨度（有则附）。"""
    span = ""
    if e.get("earliest_ts") is not None:
        span = " · span {0}..{1}".format(
            _ymd_utc(e["earliest_ts"]), _ymd_utc(e["latest_ts"]))
    return "{0}  {1} feeds · {2} comments · {3} replies · {4} media ({5}){6}".format(
        guild, e["feeds"], e["comments"], e["replies"],
        e["media_files"], _format_bytes(e["media_bytes"]), span)


def archive_tree_stats(data_root):
    """每 guild 实体树计数 + createTime 跨度（feeds/c_/r_/媒体与字节）。"""
    stats = {}
    for guild_root, guild in _iter_guild_dirs(data_root):
        entry = {"feeds": 0, "comments": 0, "replies": 0,
                 "media_files": 0, "media_bytes": 0, "months": {}}
        span = (None, None)
        for fname in os.listdir(os.path.join(guild_root, "feeds")):
            shard_dir = os.path.join(guild_root, "feeds", fname)
            if os.path.isdir(shard_dir):
                for f in os.listdir(shard_dir):
                    if not f.endswith(".json"):
                        continue
                    entry["feeds"] += 1
                    span = _see_entity(
                        entry, span, os.path.join(shard_dir, f), "feeds")
        comments_root = os.path.join(guild_root, "comments")
        if os.path.isdir(comments_root):
            for shard in os.listdir(comments_root):
                if not shard.startswith(("c_", "r_")):
                    continue
                shard_dir = os.path.join(comments_root, shard)
                if not os.path.isdir(shard_dir):
                    continue
                key = "comments" if shard.startswith("c_") else "replies"
                for f in os.listdir(shard_dir):
                    if not f.endswith(".json"):
                        continue
                    entry[key] += 1
                    span = _see_entity(
                        entry, span, os.path.join(shard_dir, f), key)
        entry["earliest_ts"], entry["latest_ts"] = span
        media_root = os.path.join(guild_root, "media")
        if os.path.isdir(media_root):
            for shard in os.listdir(media_root):
                shard_dir = os.path.join(media_root, shard)
                if not os.path.isdir(shard_dir):
                    continue
                for f in os.listdir(shard_dir):
                    try:
                        entry["media_bytes"] += os.path.getsize(
                            os.path.join(shard_dir, f))
                        entry["media_files"] += 1
                    except OSError:
                        continue
        stats[guild] = entry
    return stats


def _mask_secret(secret):
    text = str(secret or "")
    return text if len(text) <= 8 else "{0}****{1}".format(
        text[:4], text[-4:])


def _format_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "{0:.1f} {1}".format(n, unit) if unit != "B" else "{0} B".format(n)
        n /= 1024.0


class Dispatcher:
    """Execute a :class:`Cmd` against a ``ProcessManager``.

    Safe to call from multiple threads (REPL + monitor)——串行化归
    ProcessManager 内部锁。

    remote：宿主模式 pm 直控；远程模式动词经 LauncherClient
    言守护进程（pm 可为 None——shell 薄客户端零进程触碰）。
    """

    def __init__(self, pm, config, config_path, remote=None, data_root=None,
                 scraper_conf_path=None, viewer_conf_path=None,
                 guilds_conf_path=None, deepbackfill_conf_path=None):
        self.pm = pm
        self.config = config
        self.config_path = config_path
        self.data_root = data_root or _ARCHIVE_DATA_ROOT
        self.scraper_conf_path = (
            scraper_conf_path or os.path.join("conf", "prometheus.conf.json"))
        self.viewer_conf_path = (
            viewer_conf_path or os.path.join("conf", "viewer.conf.json"))
        self.guilds_conf_path = (
            guilds_conf_path or os.path.join("conf", "guilds.conf.json"))
        self.deepbackfill_conf_path = (
            deepbackfill_conf_path
            or os.path.join("conf", "deepbackfill.conf.json"))
        self.remote = remote
        self._lock = threading.Lock()

    def dispatch(self, cmd):
        """Execute ``cmd`` and return ``{"ok": bool, "message": str, "data": dict}``."""
        with self._lock:
            return self._dispatch_locked(cmd)

    def _dispatch_locked(self, cmd):
        verb = cmd.verb
        if verb == "noop":
            return {"ok": True, "message": "", "data": {}}
        if verb == "help":
            return {"ok": True, "message": HELP_TEXT, "data": {}}
        if verb == "quit":
            msg = (
                "Closing client shell — targets keep running"
                " (daemon supervises; 'shutdown' stops all)."
                if self.remote else "Shutting down..."
            )
            return {"ok": True, "message": msg, "data": {"action": "quit"}}
        if verb == "shutdown":
            return {"ok": True, "message": "", "data": {"action": "shutdown_tree"}}
        if verb == "clear":
            return {"ok": True, "message": "", "data": {"action": "clear"}}
        if verb == "health":
            return self._handle_health(cmd.noun)
        if verb == "logs":
            return {"ok": True, "message": "",
                    "data": {"action": "tail_log", "target": cmd.noun}}
        if verb == "stats":
            return self._handle_stats()
        if verb == "config":
            return self._handle_config(cmd)
        if verb == "archive":
            return self._handle_archive(cmd)
        if verb == "auth":
            return {"ok": True, "message": "",
                    "data": {"action": "auth"}}
        if verb == "start":
            return self._handle_start(cmd.noun)
        if verb == "stop":
            return self._handle_stop(cmd.noun)
        if verb == "restart":
            return self._handle_restart(cmd.noun)
        # Parser is the gatekeeper; an unknown verb here is a programming error.
        raise ValueError("Unhandled command verb: {0}".format(verb))

    # 三目标健康面：config 端口键 + 默认端口 + 探测路径。
    # viewer 无 /health（SPA fallback 恒 200）——/api/stats 才是真活体面
    # （API 层 + SQLite 双活才算 OK）。探测恒直读 localhost 端口，
    # 不经守护转发（与 scraper 自有 API 同一先例，宿主/远程模式同形）。
    _HEALTH_FACES = {
        "scraper": ("scraper_api_port", 9420, "/health"),
        "deepbackfill": ("deepbackfill_port", 9424, "/health"),
        "viewer": ("viewer_port", 9422, "/api/stats"),
    }

    def _handle_health(self, noun):
        """``health [target]``——三目标 API 健康面（docker 风格逐行报告）。

        每行 = 监督态 + restart 计数（原 status 动词并入）；目标未
        运行 → 到态为止（不探测、不等超时）；运行中 → 追加 localhost
        端口探测。裸 ``health`` = 全目标，``ok`` = 被检目标皆 OK。
        """
        targets = [noun] if noun else list(self._HEALTH_FACES)
        lines = []
        all_ok = True
        for target in targets:
            port_key, default_port, path = self._HEALTH_FACES[target]
            port = self.config.get(port_key, default_port)
            state = self._state_of(target)
            status = self._status_of(target)
            line = "{0:<12} :{1}  {2:<7}  restarts {3}".format(
                target, port, state, status.get("restarts", "?"))
            if state != "running":
                lines.append(line)
                all_ok = False
                continue
            if self._probe_http(port, path):
                lines.append(line + "  API OK")
            else:
                lines.append(line + "  API TIMEOUT")
                all_ok = False
        return {"ok": all_ok, "message": "\n".join(lines), "data": {}}

    def _handle_stats(self):
        """``stats``——每 guild 实体树计数 + total 行。"""
        root = self.data_root
        stats = archive_tree_stats(root)
        if not stats:
            return {"ok": True,
                    "message": "No archive found under {0}.".format(root),
                    "data": {}}
        lines = []
        totals = {"feeds": 0, "comments": 0, "replies": 0,
                  "media_files": 0, "media_bytes": 0}
        for guild in sorted(stats):
            e = stats[guild]
            lines.append(_guild_stat_line(guild, e))
            for ym in sorted(e["months"]):
                lines.append(_month_line(ym, e["months"][ym]))
            for k in totals:
                totals[k] += e[k]
        lines.append("total  {0} feeds · {1} comments · {2} replies ·"
                     " {3} media ({4})".format(
                         totals["feeds"], totals["comments"],
                         totals["replies"], totals["media_files"],
                         _format_bytes(totals["media_bytes"])))
        lines.append("usage: archive <guild> <from> <to> [--apply]"
                     " — window (from, to] on entity createTime"
                     " (UTC YYYYMMDD)")
        return {"ok": True, "message": "\n".join(lines), "data": stats}

    @staticmethod
    def _probe_http(port, path, timeout=3.0):
        """localhost 端口活体探测：HTTP 200 → True；拒绝/超时 → False。"""
        import urllib.request

        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:{0}{1}".format(port, path), timeout=timeout
            ) as resp:
                return resp.status == 200
        except OSError:
            return False

    def _handle_start(self, noun):
        if self._state_of(noun) == "running":
            return {"ok": False,
                    "message": "{0} is already running.".format(noun),
                    "data": {}}
        if self.remote:
            self.remote.start(noun)
        else:
            self.pm.start(noun)
        return {"ok": True, "message": "{0} started.".format(noun), "data": {}}

    def _handle_stop(self, noun):
        if self._state_of(noun) == "stopped":
            return {"ok": False,
                    "message": "{0} is already stopped.".format(noun),
                    "data": {}}
        if self.remote:
            self.remote.stop(noun)
        else:
            self.pm.stop(noun)
        return {"ok": True, "message": "{0} stopped.".format(noun), "data": {}}

    def _handle_restart(self, noun):
        if self.remote:
            self.remote.restart(noun)
        else:
            self.pm.restart(noun)
        return {"ok": True, "message": "{0} restarted.".format(noun), "data": {}}

    def _state_of(self, noun):
        if self.remote:
            return self.remote.status_of(noun).get("state", "unknown")
        return self.pm.status_of(noun)["state"]

    def _status_of(self, noun):
        if self.remote:
            return self.remote.status_of(noun)
        return self.pm.status_of(noun)

    def _handle_config(self, cmd):
        """``config show|set``——分组视图 + 键注册表路由（方案 B）。

        launcher 键 → 守护单写者（远程）或宿主原子写；
        scraper/viewer 键 → 各自 conf 原子合并写盘，scraper 运行中
        顺带同步 live 回显（行为仍须 restart——组件启动期定型）。
        """
        if cmd.noun == "show":
            if self.remote:
                self.config = self.remote.config_get()
            return {"ok": True,
                    "message": self._config_show_message(), "data": {}}
        key, raw = cmd.args[0], cmd.args[1]
        route = CONFIG_ROUTES.get(key)
        if route is None:
            return {"ok": False,
                    "message": self._unknown_key_message(key), "data": {}}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        domain, effect = route
        if domain == "launcher":
            if self.remote:
                self.config = self.remote.config_set(key, value)
            else:
                self.config[key] = value
                self._atomic_write_json(self.config_path, self.config)
            return {"ok": True,
                    "message": "launcher: {0} = {1}  [{2}]".format(
                        key, json.dumps(value, ensure_ascii=False), effect),
                    "data": {}}
        path = (self.scraper_conf_path if domain == "scraper"
                else self.viewer_conf_path)
        disk = self._read_json(path, {})
        disk[key] = value
        self._atomic_write_json(path, disk)
        extra = ""
        if domain == "scraper" and self._target_state("scraper") == "running":
            extra = (" · live echo synced"
                     if self._live_put_scraper(key, value)
                     else " · live sync failed")
        return {"ok": True,
                "message": "{0}: {1} = {2} → {3}  [{4}]{5}".format(
                    domain, key, json.dumps(value, ensure_ascii=False),
                    path, effect, extra),
                "data": {}}

    def _config_show_message(self):
        lines = ["launcher ({0}):".format(self.config_path)]
        for key in ("launcher_port", "max_restarts", "restart_delay",
                    "viewer_port", "deepbackfill_port"):
            value = self.config.get(key, _LAUNCHER_KEY_DEFAULTS[key])
            default_note = "" if key in self.config else "  (default)"
            lines.append("  {0:<26} = {1}{2}  [{3}]".format(
                key, json.dumps(value, ensure_ascii=False), default_note,
                CONFIG_ROUTES[key][1]))
        disk = self._read_json(self.scraper_conf_path, {})
        lines.append("scraper ({0})  [restart scraper to apply]:".format(
            self.scraper_conf_path))
        for key in _SCRAPER_KEYS:
            if key in disk:
                lines.append("  {0:<26} = {1}".format(
                    key, json.dumps(disk[key], ensure_ascii=False)))
        lines.append("  · {0}".format(self._scraper_live_note(disk)))
        vdisk = self._read_json(self.viewer_conf_path, {})
        lines.append("viewer ({0})  [restart viewer to apply]:".format(
            self.viewer_conf_path))
        for key in _VIEWER_KEYS:
            if key in vdisk:
                lines.append("  {0:<26} = {1}".format(
                    key, json.dumps(vdisk[key], ensure_ascii=False)))
        guilds = self._read_json(self.guilds_conf_path, {}).get("guilds", [])
        lines.append(
            "guilds ({0})  [read-only — edit file + restart scraper]:".format(
                self.guilds_conf_path))
        for entry in guilds:
            lines.append("  {0}  {1}  {2}".format(
                entry.get("guild_id", "?"),
                entry.get("guild_number", "?"),
                entry.get("name", "")))
        creds = self._read_json(self.deepbackfill_conf_path, {})
        if creds:
            lines.append("deepbackfill credentials  [auth flow owns]:")
            lines.append("  uin = {0}  p_skey = {1}  minted_at = {2}".format(
                creds.get("uin"), _mask_secret(creds.get("p_skey")),
                creds.get("minted_at")))
        return "\n".join(lines)

    def _scraper_live_note(self, disk):
        state = self._target_state("scraper")
        if state != "running":
            return "scraper {0} — live view unavailable".format(state)
        live = self._scraper_live_config()
        if live is None:
            return "live probe failed (scraper running but API unreachable)"
        diffs = [
            "{0}: disk {1} → live {2}".format(
                key, json.dumps(disk.get(key), ensure_ascii=False),
                json.dumps(live.get(key), ensure_ascii=False))
            for key in _SCRAPER_KEYS
            if key in disk and key in live and disk[key] != live[key]
        ]
        return "live matches disk" if not diffs else (
            "live drift — " + "; ".join(diffs))

    def _scraper_live_config(self, timeout=3.0):
        import urllib.request

        port = self._read_json(
            self.scraper_conf_path, {}).get("scraper_api_port", 9420)
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:{0}/config".format(port), timeout=timeout
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError):
            return None

    def _live_put_scraper(self, key, value):
        import urllib.request

        port = self._read_json(
            self.scraper_conf_path, {}).get("scraper_api_port", 9420)
        req = urllib.request.Request(
            "http://127.0.0.1:{0}/config".format(port),
            data=json.dumps({key: value}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.status == 200
        except OSError:
            return False

    def _target_state(self, name):
        try:
            return self._state_of(name)
        except Exception:
            return "unknown"

    def _unknown_key_message(self, key):
        lines = ["Unknown config key: '{0}'. Valid keys:".format(key)]
        for domain in ("launcher", "scraper", "viewer"):
            keys = sorted(k for k, (d, _e) in CONFIG_ROUTES.items()
                          if d == domain)
            lines.append("  {0}: {1}".format(domain, ", ".join(keys)))
        return "\n".join(lines)

    @staticmethod
    def _read_json(path, default):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return default

    @staticmethod
    def _atomic_write_json(path, payload):
        dir_path = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise

    def _archive_panorama(self, guild):
        """裸 archive / archive <guild>——可备份范围全景 + 用法。

        回答「有哪些时间可以备份」：每 guild createTime 跨度 + 计数，
        跨度格式与窗参同款 YYYYMMDD，看一眼即可直接拼命令。
        """
        stats = archive_tree_stats(self.data_root)
        if not stats:
            return {"ok": True,
                    "message": "No archive found under {0}.".format(
                        self.data_root),
                    "data": {}}
        if guild is not None and guild not in stats:
            return {"ok": False,
                    "message": "no data for guild {0} under {1}"
                               " — known guilds: {2}".format(
                                   guild, self.data_root,
                                   ", ".join(sorted(stats))),
                    "data": {}}
        lines = []
        for g in sorted(stats):
            if guild is not None and g != guild:
                continue
            lines.append(_guild_stat_line(g, stats[g]))
            for ym in sorted(stats[g]["months"]):
                lines.append(_month_line(ym, stats[g]["months"][ym]))
        lines.append("usage: archive <guild> <from> <to> [--apply]"
                     " [--force] [--output DIR]")
        lines.append("window (from, to] on entity createTime"
                     " — dates UTC YYYYMMDD")
        return {"ok": True, "message": "\n".join(lines), "data": stats}

    def _handle_archive(self, cmd):
        """同步直调打包引擎（批处理——engine 跑多久 shell 就等多久）。

        消息面与 scripts/archive.py CLI 逐字对齐（counts 行 / DRY RUN /
        wrote 行）；引擎的 exit-code 语义转为 shell 错误消息：
        WindowError→2 面、ReconciliationError→3 面、其余→1 面。
        engine 延迟导入——launcher 基础安装（无 [contracts] extra）不受累，
        动词被使用时才要求 zhizong 在位。
        """
        guild = cmd.noun
        from_ymd, to_ymd, opts = cmd.args
        if from_ymd is None:
            return self._archive_panorama(guild)
        try:
            from src.archive.engine import (
                ReconciliationError,
                WindowError,
                plan_package,
                write_package,
            )
        except ImportError as exc:
            return {"ok": False,
                    "message": "archive engine unavailable"
                               " (pip install -e \".[contracts]\"): {0}".format(exc),
                    "data": {}}
        try:
            plan = plan_package(self.data_root, guild, from_ymd, to_ymd)
            if plan.is_empty:
                span_note = ""
                st = archive_tree_stats(self.data_root).get(guild)
                if st and st.get("earliest_ts") is not None:
                    span_note = "  data spans {0}..{1}".format(
                        _ymd_utc(st["earliest_ts"]),
                        _ymd_utc(st["latest_ts"]))
                return {"ok": True,
                        "message": "no data in window ({0}, {1}] for guild {2}"
                                   " — nothing to archive.{3}".format(
                                       from_ymd, to_ymd, guild, span_note),
                        "data": {}}
            counts = plan.counts
            header = "window ({0}, {1}] guild {2}: feeds={3} comments={4}" \
                     " replies={5} media={6}".format(
                         from_ymd, to_ymd, guild,
                         counts["feeds"], counts["comments"],
                         counts["replies"], len(plan.media))
            if not opts["apply"]:
                return {"ok": True,
                        "message": header
                                   + "\nDRY RUN — no package written."
                                     " Use --apply to create it.",
                        "data": {}}
            output = Path(opts["output"]) if opts["output"] else _ARCHIVE_OUTPUT_ROOT
            out_path = write_package(plan, output, force=opts["force"])
            return {"ok": True,
                    "message": header
                               + "\nwrote {0} ({1:,} bytes)".format(
                                   out_path, out_path.stat().st_size),
                    "data": {}}
        except WindowError as exc:
            return {"ok": False, "message": "ERROR: {0}".format(exc), "data": {}}
        except ReconciliationError as exc:
            return {"ok": False,
                    "message": "ERROR: pre-pack reconciliation failed: {0}".format(exc),
                    "data": {}}
        except Exception as exc:  # noqa: BLE001 —— CLI 兜底同款：未知 guild/IO → 1 面
            return {"ok": False,
                    "message": "ERROR: {0}: {1}".format(type(exc).__name__, exc),
                    "data": {}}

    @staticmethod
    def _format_status_string(s):
        """Render a TargetList snapshot (from ``pm.status_all()``) multi-line."""
        labels = {
            "scraper": "Scraper",
            "deepbackfill": "Deepbackfill",
            "viewer": "Viewer",
        }
        lines = [
            "  {0:<12s} {1:8s} restart #{2}".format(
                labels[t["name"]], t["state"], t["restarts"]
            )
            for t in s["targets"]
        ]
        return "\n".join(lines)
