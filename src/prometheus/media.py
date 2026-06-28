"""Media archive: local cache copy + CDN URL catalog.

QQ caches viewed media under ``nt_data/Pic|Video|File`` with MD5-based filenames.
Post protobuf blobs reference media via CDN URLs instead of local paths, so this
module handles two concerns:

1. Copying the local media cache into the archive (best-effort, lossless).
2. Cataloguing CDN URLs from posts so missing media can be re-fetched later.

CDN accessibility (verified empirically):
- ``qqchannel-profile-*.file.myqcloud.com/<id>``      -> images, no auth, no expiry
- ``channelr.photo.store.qq.com/psc?/channel/...``     -> images, no auth, full-res
- ``qchannelvideo.photo.qq.com/<id>.mp4?dis_k=&dis_t=`` -> videos, signed URL, EXPIRES
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class MediaEntry:
    url: str
    kind: str  # "image" | "video" | "avatar"
    cached_locally: bool = False
    local_path: str = ""


@dataclass
class MediaIndex:
    copied: list[Path] = field(default_factory=list)
    entries: list[MediaEntry] = field(default_factory=list)
    total_bytes: int = 0


def classify_url(url: str) -> str:
    if "qchannelvideo" in url or "channelvideo" in url:
        return "video"
    if "qlogo" in url:
        return "avatar"
    return "image"


def strip_thumbnail(url: str) -> str:
    """Turn a myqcloud thumbnail URL into a full-res variant."""
    return re.sub(r"/\d+(\?t=\d+)?$", r"\1", url) or url


def copy_local_cache(nt_data_dir: Path, dest: Path) -> tuple[list[Path], int]:
    """Copy Pic/Video/File directories into the archive. Returns (files, total_bytes)."""
    dest.mkdir(parents=True, exist_ok=True)
    subdirs = config.get("media_subdirs") or ["Pic", "Video", "File"]
    files: list[Path] = []
    total = 0
    for subdir in subdirs:
        src = nt_data_dir / subdir
        if not src.is_dir():
            continue
        dst = dest / subdir
        for root, _dirs, fnames in os.walk(src):
            rel = Path(root).relative_to(src)
            target_dir = dst / rel
            target_dir.mkdir(parents=True, exist_ok=True)
            for fn in fnames:
                s = Path(root) / fn
                d = target_dir / fn
                if not d.exists():
                    shutil.copy2(s, d)
                    total += s.stat().st_size
                files.append(d)
    return files, total


def build_index(posts_media_urls: dict[str, list[str]], cache_files: list[Path]) -> MediaIndex:
    """Cross-reference post media URLs against copied cache files (by content MD5 in filename)."""
    cache_md5s: dict[str, Path] = {}
    for f in cache_files:
        m = re.match(r"([0-9a-f]{32})", f.name)
        if m:
            cache_md5s.setdefault(m.group(1), f)

    index = MediaIndex(copied=cache_files)
    for thread_id, urls in posts_media_urls.items():
        for url in urls:
            index.entries.append(
                MediaEntry(
                    url=url,
                    kind=classify_url(url),
                    cached_locally=False,
                )
            )
    index.total_bytes = sum(f.stat().st_size for f in cache_files if f.exists())
    return index
