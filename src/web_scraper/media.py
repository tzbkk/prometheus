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
        self._comment_index_path = self.data_dir / "comment_media_index.jsonl"
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
        # Cross-dedup: seed _seen from comment_media_index.jsonl too, so a
        # URL fetched for a feed is not re-fetched for a comment (and vv.).
        if self._comment_index_path.exists():
            before = len(self._seen)
            for line in self._comment_index_path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    url = entry.get("url")
                    if url:
                        self._seen.add(normalize_media_url(url))
                except (json.JSONDecodeError, TypeError):
                    pass
            logger.info(
                "Loaded comment-media entries (+%d new URLs into _seen)",
                len(self._seen) - before,
            )
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

    def _download_core(
        self, url: str, media_type: str
    ) -> tuple[str, str, int, bool]:
        """Download a URL to ``media/`` with retry. Returns
        ``(norm_url, filename, size, newly_downloaded)``.

        ``newly_downloaded`` is True ONLY when bytes were fetched from the
        network this call. False if the file already existed on disk or if
        the download failed (dead URL). Does NOT write any index entry —
        the caller is responsible for that.

        Filename convention: ``SHA256(norm_url)[:16] + ext`` — identical for
        feed and comment images so cross-dedup via ``_seen`` works.
        """
        norm_url = normalize_media_url(url)
        filename = (
            hashlib.sha256(norm_url.encode()).hexdigest()[:16]
            + self._guess_ext(url, media_type)
        )
        filepath = self.media_dir / filename

        if filepath.exists() and filepath.stat().st_size > 0:
            return (norm_url, filename, filepath.stat().st_size, False)
        if norm_url in self._dead:
            return (norm_url, filename, 0, False)

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
                return (norm_url, filename, len(data), True)
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
        return (norm_url, filename, 0, False)

    def _download_one(self, url: str, media_type: str, source_id: str) -> bool:
        """Download one feed media URL; append to ``media_index.jsonl`` only
        when newly fetched. Returns 1 if newly downloaded, else 0.

        Wrapper around :meth:`_download_core` that owns the feed-index write.
        Pre-existing int-return behaviour is preserved (type hint says bool
        for historical reasons — left unchanged, do NOT "fix").
        """
        norm_url, filename, size, newly = self._download_core(url, media_type)
        if not newly:
            return 0
        self._append_index(
            {
                "url": norm_url,
                "file": filename,
                "source": source_id,
                "type": media_type,
                "size": size,
            }
        )
        return 1

    def download_comment_media(self, comment: dict, feed_id: str = "") -> int:
        """Download all images referenced by a comment dict and its replies.

        Extracts URLs from ``richContents.images[*].picUrl`` and
        ``richContents.sticker.custom_face.origin_image_url``, recursing into
        ``vecReply[*]``. Each image is written to the shared ``media/`` dir
        (SHA256 filename, deduped via ``_seen``) and a row is appended to
        ``comment_media_index.jsonl`` regardless of whether the file was newly
        fetched (the comment→file mapping is needed either way).

        Returns the count of images processed (for logging/diagnostics).
        """
        comment_id = str(comment.get("id", ""))
        count = self._extract_comment_images(comment, feed_id, comment_id)
        for reply in (comment.get("vecReply") or []):
            if not isinstance(reply, dict):
                continue
            reply_id = str(reply.get("id", ""))
            count += self._extract_comment_images(reply, feed_id, reply_id)
        return count

    def _extract_comment_images(
        self, node: dict, feed_id: str, comment_id: str
    ) -> int:
        """Pull image + sticker URLs out of one comment/reply node."""
        rich = node.get("richContents") or {}
        count = 0
        for img in (rich.get("images") or []):
            if not isinstance(img, dict):
                continue
            url = img.get("picUrl", "")
            if not url:
                continue
            width = img.get("width", 0) or 0
            height = img.get("height", 0) or 0
            media_type = "gif" if img.get("is_gif", False) else "image"
            count += self._download_comment_one(
                url, media_type, comment_id, feed_id, width, height
            )
        sticker = rich.get("sticker") or {}
        face = sticker.get("custom_face") or {}
        if isinstance(face, dict):
            url = face.get("origin_image_url", "")
            if url:
                width = face.get("pic_width", 0) or 0
                height = face.get("pic_height", 0) or 0
                count += self._download_comment_one(
                    url, "sticker", comment_id, feed_id, width, height
                )
        return count

    def _download_comment_one(
        self,
        url: str,
        media_type: str,
        comment_id: str,
        feed_id: str,
        width: int,
        height: int,
    ) -> int:
        """Download one comment-image URL; ALWAYS append a
        ``comment_media_index.jsonl`` entry (the comment→file mapping is
        needed whether the bytes were just fetched or already on disk).
        Returns 1 if the file is on disk after this call, else 0.
        """
        norm_url, filename, size, newly = self._download_core(url, media_type)
        if not filename:
            return 0
        filepath = self.media_dir / filename
        # Dead URL: not fetched this call and no file on disk.
        if not newly and not filepath.exists():
            return 0
        actual_size = filepath.stat().st_size if filepath.exists() else size
        self._append_comment_index(
            {
                "comment_id": comment_id,
                "feed_id": feed_id,
                "url": norm_url,
                "file": filename,
                "type": media_type,
                "width": width,
                "height": height,
                "size": actual_size,
            }
        )
        return 1 if filepath.exists() else 0

    def _append_index(self, entry_dict) -> None:
        """Thread-safe atomic append of one entry to media_index.jsonl.

        Uses append mode + flush, which is atomic on POSIX for single-line
        writes under PIPE_BUF (4096 bytes). Same pattern as Store.
        """
        with self._lock:
            with open(self._index_path, mode="a", encoding="utf-8") as f:
                f.write(json.dumps(entry_dict, ensure_ascii=False) + "\n")
                f.flush()

    def _append_comment_index(self, entry_dict) -> None:
        """Thread-safe atomic append of one entry to comment_media_index.jsonl."""
        with self._lock:
            with open(self._comment_index_path, mode="a", encoding="utf-8") as f:
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
