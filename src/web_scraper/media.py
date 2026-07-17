"""Media downloader for feed images and videos.

Downloads from CDN URLs (picUrl for images, playUrl for videos),
names files by SHA256(url)[:16], and maintains media_index.jsonl.
Uses ThreadPoolExecutor for concurrent downloads.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.web_scraper.urlnorm import normalize_media_url

logger = logging.getLogger(__name__)


class MediaDownloader:
    """Concurrent media downloader with SHA256-based dedup and JSONL index."""

    def __init__(
        self,
        data_dir,
        max_workers: int = 10,
        shared_semaphore: threading.Semaphore | None = None,
    ):
        """Initialize downloader with data directory.

        Args:
            data_dir: Path to data directory. A ``media/`` subdir will be created.
            max_workers: Thread pool size for concurrent downloads.
            shared_semaphore: Optional ``threading.Semaphore`` used to bound
                GLOBAL HTTP concurrency across all guild contexts (plan §2.1a / I1).
                Held ONLY around the ``urllib.request.urlopen`` network call,
                not during disk writes / index updates. ``None`` (default)
                preserves single-guild behavior unchanged.
        """
        self.data_dir = Path(data_dir)
        self.media_dir = self.data_dir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self._semaphore = shared_semaphore
        self._lock = threading.Lock()
        self._index_path = self.data_dir / "media_index.jsonl"
        self._dead_path = self.data_dir / "dead_media_permanent.jsonl"
        self._seen: set[str] = set()
        self._dead: set[str] = set()
        if self._index_path.exists():
            for line in self._index_path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    url = entry.get("url")
                    if url:
                        self._seen.add(normalize_media_url(url))
                except (json.JSONDecodeError, TypeError):
                    pass
            logger.info("Loaded %d media entries from index", len(self._seen))
        if self._dead_path.exists():
            for line in self._dead_path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    url = entry.get("url")
                    if url:
                        self._dead.add(url)
                except (json.JSONDecodeError, TypeError):
                    pass
            logger.info("Loaded %d dead media URLs", len(self._dead))

    def _sem_ctx(self):
        if self._semaphore is None:
            return contextlib.nullcontext()
        return self._semaphore

    def download_feed_media(self, feed_dict) -> int:
        """Download all media referenced by a feed dict.

        Extracts ``images[*].picUrl`` and ``videos[*].playUrl`` from the feed,
        downloads each concurrently, and returns the count of **newly**
        downloaded files (cached files are skipped silently).

        Args:
            feed_dict: Feed dictionary with optional ``images``/``videos`` lists.

        Returns:
            Number of newly downloaded media files (0 if all cached or no media).
        """
        images = feed_dict.get("images", []) or []
        videos = feed_dict.get("videos", []) or []
        source_id = feed_dict.get("id", "")

        tasks: list[tuple[str, str, str]] = []
        for img in images:
            url = img.get("picUrl", "") if isinstance(img, dict) else ""
            if url:
                tasks.append((url, "image", source_id))
        for vid in videos:
            url = vid.get("playUrl", "") if isinstance(vid, dict) else ""
            if url:
                tasks.append((url, "video", source_id))

        if not tasks:
            return 0

        success = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for result in pool.map(
                lambda t: self._download_one(*t), tasks
            ):
                success += result
        return success

    @staticmethod
    def _guess_ext(url: str, media_type: str) -> str:
        if media_type == 'video':
            return '.mp4'
        return '.jpg'

    def _download_one(self, url: str, media_type: str, source_id: str) -> bool:
        """Download a single URL to the media dir with retry.

        Files are named ``SHA256(normalized_url)[:16]``. Existing files are
        skipped.  On failure, retries up to 3 times. Each successful download
        appends an entry to ``media_index.jsonl``.

        Args:
            url: CDN URL to download (with auth params).
            media_type: ``"image"`` or ``"video"``.
            source_id: Originating feed ID (for index traceability).

        Returns:
            True if the file is present on disk after this call, else False.
        """
        norm_url = normalize_media_url(url)
        filename = hashlib.sha256(norm_url.encode()).hexdigest()[:16] + self._guess_ext(url, media_type)
        filepath = self.media_dir / filename

        if filepath.exists() and filepath.stat().st_size > 0:
            return 0
        if norm_url in self._dead:
            return 0

        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"}
                )
                with self._sem_ctx():
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        data = resp.read()
                # Atomic write: temp file + os.replace
                tmp = filepath.with_suffix(filepath.suffix + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, filepath)
                with self._lock:
                    self._seen.add(norm_url)
                self._append_index(
                    {
                        "url": norm_url,
                        "file": filename,
                        "source": source_id,
                        "type": media_type,
                        "size": len(data),
                    }
                )
                return 1
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_err = exc
                logger.warning(
                    "media download attempt %d/3 failed for %s: %s",
                    attempt,
                    url,
                    exc,
                )

        logger.error("media download failed after 3 attempts: %s (%s)", url, last_err)
        with self._lock:
            if norm_url not in self._dead:
                self._dead.add(norm_url)
                with open(self._dead_path, mode="a", encoding="utf-8") as f:
                    f.write(json.dumps({"url": norm_url}) + "\n")
                    f.flush()
        return 0

    def _append_index(self, entry_dict) -> None:
        """Thread-safe atomic append of one entry to media_index.jsonl.

        Uses append mode + flush, which is atomic on POSIX for single-line
        writes under PIPE_BUF (4096 bytes). Same pattern as Store.
        """
        with self._lock:
            with open(self._index_path, mode="a", encoding="utf-8") as f:
                f.write(json.dumps(entry_dict, ensure_ascii=False) + "\n")
                f.flush()

    @staticmethod
    def classify_url(url: str) -> str:
        """Classify a CDN URL as ``"video"`` or ``"image"``.

        Detection by URL signature: QQ video CDN hosts or ``.mp4`` extension.
        """
        if "qchannelvideo" in url or "channelvideo" in url or ".mp4" in url:
            return "video"
        return "image"
