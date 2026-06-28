#!/usr/bin/env python3
"""Emit config as ``KEY=VALUE`` lines for bash sourcing.

Usage in shell scripts::

    eval "$(python3 prometheus/_envconfig.py)"

Each JSON key becomes ``PROMETHEUS_<UPPER_KEY>=<value>``. Lists and dicts
are JSON-encoded (so inject.js can ``JSON.parse`` them). ``null`` is
emitted as empty so it means "unset" downstream. Values are
shell-escaped with ``shlex.quote``.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path


def main() -> int:
    cfg_path = Path(
        os.environ.get("PROMETHEUS_CONFIG")
        or (Path(__file__).resolve().parent.parent.parent / "prometheus.conf.json")
    )
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    overrides: list[str] = []
    for key, val in raw.items():
        if key.startswith("_"):
            continue
        env_key = f"PROMETHEUS_{key.upper()}"
        if env_key in os.environ:
            continue

        if val is None:
            sval = ""
        elif isinstance(val, (list, dict)):
            sval = json.dumps(val, ensure_ascii=False)
        elif isinstance(val, bool):
            sval = "1" if val else "0"
        else:
            sval = str(val)

        overrides.append(f"{env_key}={shlex.quote(sval)}")

    sys.stdout.write("\n".join(overrides))
    if overrides:
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
