"""Tests for scripts/migrate_multi_guild.py.

Every test runs against an isolated ``tempfile.TemporaryDirectory()`` so the
real production ``data/`` is never touched.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.migrate_multi_guild import (  # noqa: E402
    _FLAT_ENTRIES,
    _MEDIA_DIR,
    migrate,
)

_GUILD = "7743321643036658"


def _make_flat_layout(data_dir: Path, *, media_files: int = 3) -> None:
    """Populate ``data_dir`` with the canonical flat layout."""
    for name in _FLAT_ENTRIES:
        (data_dir / name).write_text(f"contents of {name}\n", encoding="utf-8")
    media = data_dir / _MEDIA_DIR
    media.mkdir(parents=True, exist_ok=True)
    for i in range(media_files):
        (media / f"img_{i}.jpg").write_bytes(b"fake-jpeg-" + str(i).encode())


def test_migrate_moves_all_flat_files():
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d)
        _make_flat_layout(data_dir)
        (data_dir / "prometheus.lock").write_text("lock", encoding="utf-8")

        result = migrate(data_dir, _GUILD)

        assert result is True
        target = data_dir / _GUILD
        for name in _FLAT_ENTRIES:
            assert (target / name).is_file(), f"{name} should be in target/"
            assert not (data_dir / name).exists(), f"{name} should be gone from parent"
        assert (target / _MEDIA_DIR).is_dir()
        assert not (data_dir / _MEDIA_DIR).exists()
        assert sorted(p.name for p in (target / _MEDIA_DIR).iterdir()) == [
            "img_0.jpg",
            "img_1.jpg",
            "img_2.jpg",
        ]
        assert (data_dir / "prometheus.lock").is_file()
        assert not (target / "prometheus.lock").exists()


def test_migrate_idempotent_rerun():
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d)
        _make_flat_layout(data_dir)

        first = migrate(data_dir, _GUILD)
        assert first is True

        second = migrate(data_dir, _GUILD)
        assert second is True

        target = data_dir / _GUILD
        for name in _FLAT_ENTRIES:
            assert (target / name).is_file()
        assert (target / _MEDIA_DIR).is_dir()
        assert sorted(p.name for p in (target / _MEDIA_DIR).iterdir()) == [
            "img_0.jpg",
            "img_1.jpg",
            "img_2.jpg",
        ]


def test_migrate_resumes_after_partial():
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d)
        _make_flat_layout(data_dir)
        target = data_dir / _GUILD
        target.mkdir(parents=True, exist_ok=True)

        # Simulate a prior interrupted run: move a couple entries by hand.
        (data_dir / "feeds.jsonl").rename(target / "feeds.jsonl")
        (data_dir / "ids.json").rename(target / "ids.json")

        result = migrate(data_dir, _GUILD)
        assert result is True

        for name in _FLAT_ENTRIES:
            assert (target / name).is_file(), f"{name} should be in target/"
            assert not (data_dir / name).exists(), f"{name} should be gone from parent"
        assert (target / _MEDIA_DIR).is_dir()
        assert not (data_dir / _MEDIA_DIR).exists()


def test_migrate_media_merge_not_overwrite():
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d)
        target = data_dir / _GUILD

        # Target already has media/existing.jpg from a prior run.
        (target / _MEDIA_DIR).mkdir(parents=True, exist_ok=True)
        (target / _MEDIA_DIR / "existing.jpg").write_bytes(b"TARGET")

        # Source has the SAME file plus a new one.
        src_media = data_dir / _MEDIA_DIR
        src_media.mkdir(parents=True, exist_ok=True)
        (src_media / "existing.jpg").write_bytes(b"SOURCE")
        (src_media / "new.jpg").write_bytes(b"NEW")
        (data_dir / "feeds.jsonl").write_text("x", encoding="utf-8")

        result = migrate(data_dir, _GUILD)
        assert result is True

        assert (target / _MEDIA_DIR / "existing.jpg").read_bytes() == b"TARGET"
        assert (target / _MEDIA_DIR / "new.jpg").read_bytes() == b"NEW"


def test_migrate_rejects_empty_guild_id():
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d)
        _make_flat_layout(data_dir)

        result = migrate(data_dir, "")

        assert result is False
        # No junk subdir created: parent holds exactly the original entries.
        entries = {p.name for p in data_dir.iterdir()}
        assert entries == set(_FLAT_ENTRIES) | {_MEDIA_DIR}
        for name in _FLAT_ENTRIES:
            assert (data_dir / name).is_file(), f"{name} should be untouched"


def test_migrate_rejects_non_numeric_guild_id():
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d)
        _make_flat_layout(data_dir)

        result = migrate(data_dir, "abc")

        assert result is False
        assert not (data_dir / "abc").exists(), "non-numeric guild_id must not create a dir"
        for name in _FLAT_ENTRIES:
            assert (data_dir / name).is_file(), f"{name} should be untouched"


def test_migrate_no_source_returns_false():
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d)

        result = migrate(data_dir, _GUILD)

        assert result is False
        target = data_dir / _GUILD
        # Target may have been created (mkdir exist_ok) but must be empty.
        if target.exists():
            assert list(target.iterdir()) == []


def test_migrate_preserves_lock():
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d)
        (data_dir / "prometheus.lock").write_text("lock-content", encoding="utf-8")
        (data_dir / "feeds.jsonl").write_text("feeds", encoding="utf-8")

        result = migrate(data_dir, _GUILD)

        assert result is True
        assert (data_dir / "prometheus.lock").read_text(encoding="utf-8") == "lock-content"
        assert (data_dir / _GUILD / "feeds.jsonl").is_file()
        assert not (data_dir / _GUILD / "prometheus.lock").exists()
