"""Configuration loader for Prometheus.

Single source of truth: ``prometheus.conf.json`` at the project root.
Every module reads values through :func:`load` (or :func:`get` for one key).

Environment overrides take precedence — useful for one-off runs without
editing the file, and as the bridge that bash scripts use to pass values
into ``inject.js`` (which runs inside QQ's Electron process and cannot
read the JSON file directly after being copied into the AppImage).

Schema overview (see ``prometheus.conf.json`` for canonical defaults):

- ``channel_id`` / ``channel_name`` / ``guild_menu_text`` — target channel
- ``feed_id_prefix`` / ``scan_depth`` / ``scan_array_depth`` — feed matching
- ``url_guild_page`` / ``url_hidden_window`` / ``url_channel_page`` — URL matchers
- ``data_dir`` / ``patched_dir`` / ``appimage`` — paths (``~`` and ``null`` supported)
- ``ydotool_bin`` / ``ydotool_socket`` — autoscroll
- ``qq_version`` / ``qq_cmdline_markers`` / ``ozone_platform`` — QQ runtime
- ``pseudonymize_salt`` — anonymization
- ``scroll_*`` / ``autoscroll_*`` — scrolling tunables
- ``cdp_host`` / ``cdp_port`` — CDP (scraper.py, currently unused)
- ``media_subdirs`` — local cache directory names
- ``startup_sequence`` — list of ``{"action": ..., "delay_ms": ...}`` for inject.js
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "prometheus.conf.json"


def _expand(p):
    if p is None or p == "":
        return None
    return Path(p).expanduser()


def config_path() -> Path:
    return Path(os.environ.get("PROMETHEUS_CONFIG") or DEFAULT_CONFIG_PATH)


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    """Load and cache the config dict.

    Env vars ``PROMETHEUS_<UPPER_KEY>`` override file values when set.
    List/dict values come through as JSON strings; scalars are coerced
    to the file value's type (bool/int/float), and ``None`` (null) is
    preserved when the override is the empty string — this lets callers
    explicitly mean "auto-detect" via ``PROMETHEUS_FOO=``.
    """
    raw = json.loads(config_path().read_text(encoding="utf-8"))
    cfg = {k: v for k, v in raw.items() if not k.startswith("_")}

    for key in list(cfg.keys()):
        env_key = f"PROMETHEUS_{key.upper()}"
        if env_key not in os.environ:
            continue
        sval = os.environ[env_key]
        old = cfg[key]
        if isinstance(old, (list, dict)):
            try:
                cfg[key] = json.loads(sval)
            except (json.JSONDecodeError, TypeError):
                cfg[key] = sval
        elif isinstance(old, bool):
            cfg[key] = sval.lower() in ("1", "true", "yes", "on")
        elif isinstance(old, int) and not isinstance(old, bool):
            try:
                cfg[key] = int(sval)
            except ValueError:
                cfg[key] = old
        elif isinstance(old, float):
            try:
                cfg[key] = float(sval)
            except ValueError:
                cfg[key] = old
        elif old is None:
            cfg[key] = None if sval == "" else sval
        else:
            cfg[key] = sval
    return cfg


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def expanded_path(key: str, fallback=None) -> Path | None:
    val = get(key)
    if val:
        p = _expand(val)
        if p and not p.is_absolute():
            p = _PROJECT_ROOT / p
        return p
    return _expand(fallback) if fallback is not None else None


def data_dir() -> Path:
    """Resolved data directory.

    Precedence: ``data_dir`` config → ``PROMETHEUS_DATA`` env (legacy) →
    ``<project>/data``.
    """
    d = expanded_path("data_dir")
    if d:
        return d
    legacy = os.environ.get("PROMETHEUS_DATA")
    if legacy:
        return Path(legacy)
    return _PROJECT_ROOT / "data"


def patched_dir() -> Path:
    """Resolved patched-QQ directory.

    Precedence: ``patched_dir`` config (absolute or relative-to-project) →
    ``<project>/qq_patched``. Default kept inside the project so AppImage
    extraction doesn't pollute ``$HOME``.
    """
    d = expanded_path("patched_dir")
    if d:
        return d
    return _PROJECT_ROOT / "qq_patched"


def output_dir() -> Path:
    return _PROJECT_ROOT / "output"


def project_root() -> Path:
    return _PROJECT_ROOT


def reset_cache() -> None:
    load.cache_clear()
