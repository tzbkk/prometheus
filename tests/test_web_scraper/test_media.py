"""Tests for MediaDownloader.

All network access is mocked — no real HTTP calls are made.
"""

import hashlib
import json
import os
import sys

from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.media import MediaDownloader


def _mock_urlopen(data: bytes = b"fake-image-bytes"):
    """Build a MagicMock that behaves like urlopen's context manager."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = data
    cm.status = 200
    return cm


def _make_feed(images=None, videos=None, feed_id="B_test1"):
    feed: dict = {"id": feed_id}
    if images:
        feed["images"] = [{"picUrl": u} for u in images]
    if videos:
        feed["videos"] = [{"playUrl": u} for u in videos]
    return feed


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_download_feed_media_downloads_images(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _mock_urlopen(b"img-bytes")
    dl = MediaDownloader(tmp_path)
    feed = _make_feed(images=["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"])

    count = dl.download_feed_media(feed)

    assert count == 2
    files = list(dl.media_dir.iterdir())
    assert len(files) == 2
    lines = dl._index_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_download_feed_media_downloads_videos(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _mock_urlopen(b"vid-bytes")
    dl = MediaDownloader(tmp_path)
    feed = _make_feed(videos=["https://video.example.com/v1.mp4"])

    count = dl.download_feed_media(feed)

    assert count == 1
    entry = json.loads(dl._index_path.read_text(encoding="utf-8").strip())
    assert entry["type"] == "video"


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_download_feed_media_skips_empty_urls(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _mock_urlopen()
    dl = MediaDownloader(tmp_path)
    feed = _make_feed(images=["", "https://cdn.example.com/real.jpg"])

    count = dl.download_feed_media(feed)

    assert count == 1
    assert mock_urlopen.call_count == 1


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_sha256_naming(mock_urlopen, tmp_path):
    url = "https://cdn.example.com/pic.jpg"
    mock_urlopen.return_value = _mock_urlopen(b"data")
    dl = MediaDownloader(tmp_path)

    dl.download_feed_media(_make_feed(images=[url]))

    expected = hashlib.sha256(url.encode()).hexdigest()[:16] + ".jpg"
    assert (dl.media_dir / expected).exists()
    entry = json.loads(dl._index_path.read_text(encoding="utf-8").strip())
    assert entry["file"] == expected


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_skip_existing_file(mock_urlopen, tmp_path):
    url = "https://cdn.example.com/cached.jpg"
    expected = hashlib.sha256(url.encode()).hexdigest()[:16] + ".jpg"
    dl = MediaDownloader(tmp_path)
    (dl.media_dir / expected).write_bytes(b"already-there")

    count = dl.download_feed_media(_make_feed(images=[url]))

    assert count == 0
    mock_urlopen.assert_not_called()
    assert not dl._index_path.exists()


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_retry_on_failure(mock_urlopen, tmp_path):
    import urllib.error
    fake_url = "https://cdn.example.com/flaky.jpg"
    mock_urlopen.side_effect = [
        urllib.error.URLError("fail1"),
        urllib.error.URLError("fail2"),
        _mock_urlopen(b"third-time"),
    ]
    dl = MediaDownloader(tmp_path)

    ok = dl._download_one(fake_url, "image", "B_x")

    assert ok == 1
    assert mock_urlopen.call_count == 3
    expected = hashlib.sha256(fake_url.encode()).hexdigest()[:16] + ".jpg"
    assert (dl.media_dir / expected).read_bytes() == b"third-time"


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_retry_exhausted_returns_false(mock_urlopen, tmp_path):
    import urllib.error
    mock_urlopen.side_effect = urllib.error.URLError("always fails")
    dl = MediaDownloader(tmp_path)

    ok = dl._download_one("https://cdn.example.com/bad.jpg", "image", "B_x")

    assert ok == 0
    assert mock_urlopen.call_count == 3
    assert "https://cdn.example.com/bad.jpg" in dl._dead
    assert (tmp_path / "dead_media_permanent.jsonl").exists()


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_dead_url_not_retried(mock_urlopen, tmp_path):
    import urllib.error
    dl = MediaDownloader(tmp_path)
    dl._dead.add("https://cdn.example.com/dead.jpg")

    ok = dl._download_one("https://cdn.example.com/dead.jpg", "image", "B_x")

    assert ok == 0
    mock_urlopen.assert_not_called()


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_media_index_format(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _mock_urlopen(b"abc123")
    dl = MediaDownloader(tmp_path)
    url = "https://cdn.example.com/x.jpg"

    dl.download_feed_media(_make_feed(images=[url], feed_id="B_format"))

    entry = json.loads(dl._index_path.read_text(encoding="utf-8").strip())
    for key in ("url", "file", "source", "type", "size"):
        assert key in entry, f"missing key: {key}"
    assert entry["url"] == url
    assert entry["source"] == "B_format"
    assert entry["type"] == "image"
    assert entry["size"] == 6
    assert entry["file"] == hashlib.sha256(url.encode()).hexdigest()[:16] + ".jpg"


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_concurrent_downloads(mock_urlopen, tmp_path):
    urls = [f"https://cdn.example.com/{i}.jpg" for i in range(10)]
    mock_urlopen.return_value = _mock_urlopen(b"img")
    dl = MediaDownloader(tmp_path, max_workers=4)

    count = dl.download_feed_media(_make_feed(images=urls))

    assert count == 10
    assert len(list(dl.media_dir.iterdir())) == 10
    lines = dl._index_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 10


def test_classify_url():
    assert MediaDownloader.classify_url("https://qchannelvideo.photo.qq.com/x.mp4") == "video"
    assert MediaDownloader.classify_url("https://channelvideo.photo.qq.com/y") == "video"
    assert MediaDownloader.classify_url("https://cdn.example.com/clip.mp4") == "video"
    assert MediaDownloader.classify_url("https://qqchannel-profile.file.myqcloud.com/p.jpg") == "image"
