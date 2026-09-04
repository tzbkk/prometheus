"""媒体下载器（池化）：内容寻址落盘 + 状态机经 writer 晋升。

媒体引用内嵌于实体 _p.media，下载状态机改写唯一入口 = store.rewrite_media →
write_entity(touch_clocks=False)（媒体尝试不动实体观测时钟）。

内容寻址：文件名 = sha256(内容) 64 位摘要 + magic sniff 扩展名
（jpg|png|mp4，MediaAsset grammar）；分片 = 摘要前 2 位（paths.media_path）。
同内容跨实体天然去重（目标文件已存在 → 跳过字节写，仅状态机置 ok）。
sniff 不识别的字节（如 gif——grammar 无此扩展名）→ 状态 failed 留档，
retries 累积 ≥3 由 writer 晋升 dead（契约纪律优先于转码/改名猜测）。

重试语义：一次 attempt = 一次 HTTP GET（fetch 缝），失败 retries += 1、
status=failed——无调用内重试循环，跨 daemon 周期累积至晋升（与
"retries：失败重试计数（晋升 dead 的判据）"的账本语义一致）。

媒体下载后台线程池化——``attempt_entity_media``
变投递（MediaDownloadPool.enqueue per-entity job），列表观测零阻塞，评论
阶段紧随列表完成（不被内联媒体下载挡路）。池 = 有界 ThreadPoolExecutor
（max_workers 复用配置 scraper_max_workers，默认 10）；去重键 =
guild/entity_id（in-flight 集）——同一实体排队/执行中不重复投递，队列长度
天然有界（≤ 唯一实体数）；终态媒体（ok/dead）由 attempt 循环投递前过滤
（只碰 pending/failed，"投递前去重"）。并发写安全：实体改写全经
store._lock 串行 + writer._merge_media 按 url 与盘上存量合并（并发观测
新增块不丢，"media 只填不删"在池化竞争下仍成立）。池线程异常必须
logging.exception（禁裸吞噬）。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from src.entity_store.paths import media_path

__all__ = ["MediaDownloader", "MediaDownloadPool", "sniff_ext"]

logger = logging.getLogger(__name__)

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MP4_BOX = b"ftyp"
_GIF_MAGIC = b"GIF8"  # GIF87a/GIF89a 同前缀（channelr psc 动图贴纸）

_FETCH_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError)

_RETRYABLE_STATUSES = ("pending", "failed")


def sniff_ext(data: bytes) -> str | None:
    """magic sniff → jpg|png|mp4|gif；不识别 → None（fail loud，不以 URL 猜测）。"""
    if data.startswith(_JPEG_MAGIC):
        return "jpg"
    if data.startswith(_PNG_MAGIC):
        return "png"
    if len(data) >= 8 and data[4:8] == _MP4_BOX:
        return "mp4"
    if data.startswith(_GIF_MAGIC):
        return "gif"
    return None


def content_name(data: bytes) -> str | None:
    """sha256 摘要 + sniff 扩展名 → 契约媒体文件名；不可识别 → None。"""
    ext = sniff_ext(data)
    if ext is None:
        return None
    return f"{hashlib.sha256(data).hexdigest()}.{ext}"


class MediaDownloadPool:
    """有界后台媒体下载池：enqueue 投递 → 池线程执行真实下载。

    - 有界性：max_workers（= scraper_max_workers）为并发上界；in-flight
      去重集使队列长度 ≤ 唯一实体数——无洪峰面、无需独立队列上限。
    - 投递幂等：同 (guild, entity_id) 在队列/执行中 → 第二次 enqueue
      返回 False（首任务覆盖该实体全部 pending/failed 媒体；投递期间并发
      新增的 url 由下一 daemon 周期补投——跨周期累积语义不变）。
    - 停机 drain：shutdown(timeout) 关门 → 等待在途任务（timeout 上限）
      → 取消仍未启动的排队任务；执行中 HTTP 有 60s 自超时 + 原子写纪律
      （tmp + os.replace，无半写文件），池线程经 concurrent.futures
      atexit join 退出——不留孤儿线程。
    """

    def __init__(self, *, max_workers: int = 10):
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="media-pool"
        )
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._futures: set[concurrent.futures.Future] = set()
        self._submitted = 0
        self._completed = 0
        self._closed = False
        self._shutdown_report: tuple[int, int, int] | None = None

    # ------------------------------------------------------------------
    # 观测面（stats/日志）
    # ------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def depth(self) -> int:
        """在途任务数（排队 + 执行中）。"""
        with self._lock:
            return len(self._futures)

    # ------------------------------------------------------------------
    # 投递面
    # ------------------------------------------------------------------
    def enqueue(self, downloader: "MediaDownloader", entity_id: str) -> bool:
        """投递单实体媒体任务；新受理 True，in-flight 去重/已关池 False（永不 raise）。"""
        key = f"{downloader.store.guild}/{entity_id}"
        with self._lock:
            if self._closed or key in self._inflight:
                return False
            self._inflight.add(key)
        try:
            future = self._executor.submit(self._run_one, downloader, entity_id)
        except RuntimeError:  # executor 已 shutdown 的关门竞态
            with self._lock:
                self._inflight.discard(key)
            logger.warning("media pool closed — enqueue dropped for %s", key)
            return False
        with self._lock:
            self._futures.add(future)
            self._submitted += 1
        future.add_done_callback(lambda f, k=key: self._retire(f, k))
        return True

    def enqueue_all(self, downloader: "MediaDownloader", entity_ids) -> int:
        """批量投递（启动扫描存量种子面）；返回受理数。"""
        accepted = 0
        for entity_id in entity_ids:
            if self.enqueue(downloader, entity_id):
                accepted += 1
        return accepted

    # ------------------------------------------------------------------
    # drain / 停机面
    # ------------------------------------------------------------------
    def wait_all(self, timeout: float | None = None) -> tuple[int, int]:
        """等待在途任务 settle；返回 (累计完成数, 仍在途数)——测试/join 面，不关门。

        完成数取池生命期累计（时序免疫：先完成再 wait 的任务不丢账）。
        """
        with self._lock:
            pending = set(self._futures)
        if pending:
            concurrent.futures.wait(pending, timeout=timeout)
        with self._lock:
            return self._completed, len(self._futures)

    def shutdown(self, timeout: float | None = None) -> tuple[int, int, int]:
        """停机 drain（幂等）：关门 → 等待在途 → 取消排队；返回 (完成, 取消, 仍在执行)。

        timeout=None = 全量 drain（--once 语义：等完为止）；超时后仍在执行
        的任务继续跑完（60s HTTP 自超时兜底）再随 atexit join 退出。
        """
        with self._lock:
            if self._closed:
                return self._shutdown_report or (0, 0, 0)
            self._closed = True
            pending = set(self._futures)
        done, not_done = concurrent.futures.wait(pending, timeout=timeout)
        self._executor.shutdown(wait=False, cancel_futures=True)
        cancelled = sum(1 for f in not_done if f.cancelled())
        running = len(not_done) - cancelled
        self._shutdown_report = (len(done), cancelled, running)
        if not_done:
            logger.warning(
                "media pool shutdown: %d done, %d cancelled, %d still running",
                len(done),
                cancelled,
                running,
            )
        else:
            logger.info("media pool shutdown: %d job(s) drained clean", len(done))
        return self._shutdown_report

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _retire(self, future: concurrent.futures.Future, key: str) -> None:
        with self._lock:
            self._inflight.discard(key)
            self._futures.discard(future)
            self._completed += 1

    def _run_one(self, downloader: "MediaDownloader", entity_id: str) -> None:
        try:
            downloader.attempt_entity_media_now(entity_id)
        except Exception:
            logger.exception("media pool job failed for entity=%s", entity_id)


class MediaDownloader:
    """实体媒体状态机的尝试侧：pending/failed → 下载 → ok/failed →（writer）dead。

    fetch 缝（最外层）：``fetch(url) -> bytes``——测试注入合成 CDN；
    默认 urllib 单次 GET（timeout 60s，无内部重试，见模块 docstring）。

    双面：``attempt_entity_media`` = 投递面（池在位 → enqueue 立即返回；
    无池 → 内联同步，单测构造用）；``attempt_entity_media_now`` =
    同步执行面（池线程调用，真实下载 + 状态机推进）。
    """

    def __init__(
        self,
        store,
        *,
        fetch=None,
        semaphore: threading.Semaphore | None = None,
        pool: MediaDownloadPool | None = None,
    ):
        self.store = store
        self.downloaded_count = 0
        self._count_lock = threading.Lock()  # 池线程并发自增纪律（/stats 读数一致）
        self._semaphore = semaphore
        self._pool = pool
        if fetch is not None:
            self._fetch = fetch
        else:
            self._fetch = self._http_fetch

    def _http_fetch(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with self._sem():
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()

    def _sem(self):
        if self._semaphore is None:
            return contextlib.nullcontext()
        return self._semaphore

    def attempt_entity_media(self, entity_id: str) -> int:
        """投递面：池在位 → enqueue 立即返回（零阻塞——列表观测/
        评论路径不被媒体下载挡路；计数异步累计，返回值恒 0）；
        无池（单测内联构造）→ 同步执行（返回落盘数）。
        """
        if self._pool is not None:
            self._pool.enqueue(self, entity_id)
            return 0
        return self.attempt_entity_media_now(entity_id)

    def attempt_entity_media_now(self, entity_id: str) -> int:
        """同步执行面（池线程调用）：尝试该实体全部 pending/failed 媒体。

        - 死集先查：url ∈ store.dead_urls → 块直接置 dead，零网络调用
          （"死 URL 集先查"，一机制两用）；
        - 投递前去重：仅 status ∈ {pending, failed} 的块进入尝试
          （ok/dead 终态零重复投递/零重复网络）；
        - 成功 → 原子写 media/{shard}/{sha256}.{ext}，块置 ok + file 名；
        - 失败（网络错/sniff 不识别）→ retries += 1、status=failed；
        - 有任何状态变动 → store.rewrite_media 回写（writer 晋升 + 时钟冻结；
          并发安全 = store._lock 串行 + writer 按 url 与盘上存量合并）。
        """
        blocks = self.store.media_blocks(entity_id)
        if blocks is None:
            return 0

        now = self.store.now_ms()
        changed = False
        downloaded = 0

        for entry in blocks:
            if entry["status"] not in _RETRYABLE_STATUSES:
                continue
            url = entry["url"]
            fetch_url = entry.get("download_url") or url
            if url in self.store.dead_urls:
                if entry["status"] != "dead":
                    entry["status"] = "dead"
                    changed = True
                continue

            entry["retries"] = entry.get("retries", 0) + 1
            entry["last_attempt_ts"] = now
            changed = True
            try:
                data = self._fetch(fetch_url)
                name = content_name(data)
                if name is None:
                    logger.warning(
                        "media %s: content type unrecognized by magic sniff — failed",
                        url,
                    )
                    entry["status"] = "failed"
                    continue
                target = media_path(self.store.data_root, self.store.guild, name)
                if not target.exists():
                    self._atomic_write_bytes(target, data)
                entry["file"] = name
                entry["status"] = "ok"
                downloaded += 1
                with self._count_lock:
                    self.downloaded_count += 1
            except _FETCH_ERRORS as exc:
                logger.warning("media fetch failed for %s: %s", fetch_url, exc)
                entry["status"] = "failed"

        if changed:
            self.store.rewrite_media(entity_id, blocks)
        return downloaded

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """tmp + os.replace（writer/lock 同款原子写惯例）。

        tmp 名带线程标识：媒体字节写在 store 锁外由多池线程并发执行，
        同内容（同 sha 目标）并发落盘会互吃固定 .tmp——线程唯一 tmp
        下两次 replace 各自原子、字节相同、终态一致。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
