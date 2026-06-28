"""Extract SQLCipher database keys from a running QQ process's memory.

The NTQQ client stores per-account databases in SQLCipher format. Each database
has a unique 16-byte salt (stored at the file's offset 1024) and a 32-byte key
that only exists in the QQ process memory as a raw-key string of the form
``x'<64hex_key><32hex_salt>'``.

This module scans ``/proc/<pid>/mem`` for those patterns and cross-validates
against the known salts read from the database files on disk.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from . import config


def find_qq_main_pid() -> int:
    """Return the PID of the QQ main process (the one running the AppImage/qq binary)."""
    markers = config.get("qq_cmdline_markers", ["/.mount_QQ", "/opt/QQ"])
    best_pid = 0
    best_rss = 0
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        if not any(m in cmdline for m in markers):
            continue
        if "--type=" in cmdline:
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
                        if rss > best_rss:
                            best_rss = rss
                            best_pid = pid
                        break
        except (FileNotFoundError, ProcessLookupError):
            continue
    if not best_pid:
        raise RuntimeError(
            "QQ main process not found. Is QQ running and logged in?"
        )
    return best_pid


@dataclass
class DbKey:
    salt: str
    key: str
    source: str


def collect_db_salts(nt_db_dir: Path) -> dict[str, list[str]]:
    """Read the salt (first 16 bytes after the 1024-byte NTQQ header) of every .db file."""
    salts: dict[str, list[str]] = {}
    for db_path in sorted(nt_db_dir.glob("*.db")):
        try:
            with open(db_path, "rb") as f:
                f.seek(1024)
                salt_bytes = f.read(16)
            if len(salt_bytes) < 16:
                continue
            salt_hex = salt_bytes.hex()
            salts.setdefault(salt_hex, []).append(db_path.name)
        except (PermissionError, OSError):
            continue
    return salts


def scan_keys(pid: int, target_salts: set[str]) -> dict[str, str]:
    """Scan the process memory for raw-key strings matching the target salts.

    Returns a mapping ``{salt_hex: key_hex}``.
    """
    salt_bytes = {s.encode(): s for s in target_salts}
    found: dict[str, str] = {}

    fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    chunk = 4 * 1024 * 1024
    prev_tail = b""
    total = 0

    regions: list[tuple[int, int]] = []
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (r)", line)
            if not m:
                continue
            start, end = int(m.group(1), 16), int(m.group(2), 16)
            if 4096 <= end - start <= 256 * 1024 * 1024:
                regions.append((start, end))

    for start, end in regions:
        offset = start
        while offset < end:
            n = min(chunk, end - offset)
            try:
                data = os.pread(fd, n, offset)
            except OSError:
                offset += n
                continue
            if not data:
                offset += n
                continue
            total += len(data)
            buf = prev_tail + data
            for needle, salt_hex in salt_bytes.items():
                if salt_hex in found:
                    continue
                pos = buf.find(needle)
                if pos < 0:
                    continue
                ctx_start = max(0, pos - 68)
                ctx = buf[ctx_start : pos + 34]
                mt = re.search(
                    rb"x'([0-9a-f]{64})" + re.escape(needle) + rb"'", ctx
                )
                if mt:
                    found[salt_hex] = mt.group(1).decode()
            offset += n
            prev_tail = data[-128:]

    os.close(fd)
    return found


def extract_keys(nt_db_dir: Path, pid: int | None = None) -> dict[str, DbKey]:
    """High-level: collect salts, scan memory, return verified keys.

    ``nt_db_dir`` is typically ``~/.config/QQ/nt_qq_<account_hash>/nt_db``.
    """
    if pid is None:
        pid = find_qq_main_pid()

    salts = collect_db_salts(nt_db_dir)
    print(f"  发现 {len(salts)} 个唯一 salt (对应 {sum(len(v) for v in salts.values())} 个数据库)")

    key_map = scan_keys(pid, set(salts.keys()))
    print(f"  内存扫描完成,匹配 {len(key_map)}/{len(salts)} 个密钥")

    result: dict[str, DbKey] = {}
    for salt_hex, dbs in salts.items():
        if salt_hex in key_map:
            result[salt_hex] = DbKey(
                salt=salt_hex, key=key_map[salt_hex], source=", ".join(dbs)
            )
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m prometheus.keyring <nt_db_dir>")
        sys.exit(1)
    keys = extract_keys(Path(sys.argv[1]))
    for k in keys.values():
        print(f"  {k.key[:16]}...  salt={k.salt[:16]}...  [{k.source}]")
