"""审计脚本 mandate 测试（MD-031 ~ MD-040，pillar 对应面 全绿 + 播坏副本逐类点名非零）。

契约溯源：structures/{Feed,Comment,Reply,MediaAsset}.yaml（Location 分片 + MediaFile 语法 +
_p 全钉与枚举）+  范式 B / §四 写入规范 / §八（审计为 prometheus 侧工具）。

绿半边直接审计 contracts/fixtures（只读）；红半边把 fixtures guild 树复制进 tmp_path
后播种**单类**违例，断言恰一条该类违例点名被播坏文件 + CLI exit 1——契约树零写盘
（播坏副本一律在 tmp，Must-NOT-do 红线）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from scripts.audit_tree import (
    CORRUPT_JSON,
    GUILD_MISMATCH,
    ID_MISMATCH,
    MEDIA_NAME_INVALID,
    P_KEY_ORDER,
    P_MISSING,
    P_NOT_LAST,
    SCHEMA_FAIL,
    SHARD_MISMATCH,
    audit_tree,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "contracts" / "fixtures"
GUILD_TREE = FIXTURES / "data" / "1000000000000001"

FEED_B_DF = "feeds/B_df/B_9d8c7b6a5f4e3d2c1b0a9988776655443322110f.json"
FEED_B_FA = "feeds/B_fa/B_5f2e8a3b9c1d4e6f7a8b9c0d1e2f3a4b5c6d7e.json"
COMMENT = "comments/c_20/c_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a.json"
REPLY = "comments/r_7d/r_0f9e8d7c6b5a4938271605f4e3d2c1b0a99887.json"


def _copied_tree(tmp_path: Path) -> Path:
    """fixtures guild 树 → tmp 播坏副本（原树只读；数据根 = <tmp>/data）。"""
    root = tmp_path / "data"
    shutil.copytree(GUILD_TREE, root / "1000000000000001")
    return root


def _rewrite(root: Path, rel: str, mutate) -> None:
    path = root / "1000000000000001" / rel
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def _seed_and_audit(tmp_path: Path, seed) -> tuple:
    root = _copied_tree(tmp_path)
    seed(root)
    report = audit_tree(root)
    return report, main(["--data-root", str(root)])


def test_fixtures_tree_audits_clean():
    report = audit_tree(FIXTURES)
    assert report.violations == ()
    assert report.guild_roots == (Path("data/1000000000000001"),)
    assert report.entities_checked == 4
    assert report.media_checked == 3
    assert report.outside_files == (
        Path("archives/1000000000000001/packages/20260827T000000Z_full.tar.zst"),
        Path("data/prometheus.lock"),
    )
    assert main(["--data-root", str(FIXTURES)]) == 0


def test_seeded_corrupt_json_named_and_nonzero(tmp_path):
    def seed(root):
        (root / "1000000000000001" / COMMENT).write_text("{not json at all", encoding="utf-8")

    report, rc = _seed_and_audit(tmp_path, seed)
    assert [v.category for v in report.violations] == [CORRUPT_JSON]
    assert str(report.violations[0].path).endswith(COMMENT)
    assert rc == 1


def test_seeded_missing_p_named_and_nonzero(tmp_path):
    report, rc = _seed_and_audit(tmp_path, lambda r: _rewrite(r, REPLY, lambda d: d.pop("_p")))
    assert [v.category for v in report.violations] == [P_MISSING]
    assert str(report.violations[0].path).endswith(REPLY)
    assert rc == 1


def test_seeded_p_not_last_named_and_nonzero(tmp_path):
    def seed(root):
        def hoist(doc):
            reordered = {"_p": doc["_p"]}
            reordered.update({k: v for k, v in doc.items() if k != "_p"})
            doc.clear()
            doc.update(reordered)

        _rewrite(root, FEED_B_DF, hoist)

    report, rc = _seed_and_audit(tmp_path, seed)
    assert [v.category for v in report.violations] == [P_NOT_LAST]
    assert str(report.violations[0].path).endswith(FEED_B_DF)
    assert rc == 1


def test_seeded_p_key_order_named_and_nonzero(tmp_path):
    def seed(root):
        def shuffle(doc):
            p = doc["_p"]
            doc["_p"] = {
                "last_seen": p["last_seen"],
                "first_seen": p["first_seen"],
                "captured_via": p["captured_via"],
                "media": p["media"],
            }

        _rewrite(root, FEED_B_FA, shuffle)

    report, rc = _seed_and_audit(tmp_path, seed)
    assert [v.category for v in report.violations] == [P_KEY_ORDER]
    assert str(report.violations[0].path).endswith(FEED_B_FA)
    assert rc == 1


def test_seeded_shard_mismatch_named_and_nonzero(tmp_path):
    def seed(root):
        guild = root / "1000000000000001"
        wrong_bucket = guild / "feeds" / "B_aa"
        wrong_bucket.mkdir()
        shutil.copyfile(guild / FEED_B_FA, wrong_bucket / Path(FEED_B_FA).name)

    report, rc = _seed_and_audit(tmp_path, seed)
    assert [v.category for v in report.violations] == [SHARD_MISMATCH]
    assert "feeds/B_aa" in str(report.violations[0].path)
    assert rc == 1


def test_seeded_id_mismatch_named_and_nonzero(tmp_path):
    def seed(root):
        _rewrite(root, FEED_B_FA, lambda d: d.update(id="B_" + "0" * 32))

    report, rc = _seed_and_audit(tmp_path, seed)
    assert [v.category for v in report.violations] == [ID_MISMATCH]
    assert str(report.violations[0].path).endswith(FEED_B_FA)
    assert rc == 1


def test_seeded_guild_mismatch_named_and_nonzero(tmp_path):
    def seed(root):
        def reroot(doc):
            doc["channelInfo"]["sign"]["guild_id"] = "9999999999999999"

        _rewrite(root, FEED_B_FA, reroot)

    report, rc = _seed_and_audit(tmp_path, seed)
    assert [v.category for v in report.violations] == [GUILD_MISMATCH]
    assert str(report.violations[0].path).endswith(FEED_B_FA)
    assert rc == 1


def test_seeded_media_name_invalid_named_and_nonzero(tmp_path):
    def seed(root):
        bucket = root / "1000000000000001" / "media" / "ab"
        bucket.mkdir()
        (bucket / "not-content-addressed.jpg").write_bytes(b"\x89PNG\r\n")

    report, rc = _seed_and_audit(tmp_path, seed)
    assert [v.category for v in report.violations] == [MEDIA_NAME_INVALID]
    assert str(report.violations[0].path).endswith("media/ab/not-content-addressed.jpg")
    assert rc == 1


def test_seeded_schema_fail_named_and_nonzero(tmp_path):
    def seed(root):
        entry = {
            "url": "https://channel.photo.store.qq.com/x.png",
            "file": None,
            "type": "image",
            "width": None,
            "height": None,
            "status": "deas",
            "retries": 0,
            "last_attempt_ts": None,
        }

        def plant(doc):
            doc["_p"]["media"] = [entry]

        _rewrite(root, FEED_B_FA, plant)

    report, rc = _seed_and_audit(tmp_path, seed)
    assert [v.category for v in report.violations] == [SCHEMA_FAIL]
    assert str(report.violations[0].path).endswith(FEED_B_FA)
    assert "deas" in report.violations[0].detail
    assert rc == 1
