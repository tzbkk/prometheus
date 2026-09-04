#!/usr/bin/env python3
"""归档打包 CLI——时间窗
(from, to] 打包，引擎居所 src/archive/engine.py（打包是批处理
操作，不是常驻服务——:9423 服务面已拆除）。

窗口语义：from/to = UTC YYYYMMDD；三类实体各自按自身
createTime 判窗（(from, to] 左开右闭，整日闭端）；checkpoint 引用式废除。

用法::

    python scripts/archive.py --guild 7743321643036658 \\
        --from 20220101 --to 20260830 [--apply] [--force]
        [--output DIR] [--data-root DIR] [--level N]

默认 DRY-RUN（打印窗内计数，不落包）；--apply 写包至
{output}/{guild}/packages/<时刻>_from_<from>_to_<to>.tar.zst（原子落盘）。
空窗（三类计数皆零）不落包，exit 0。对 data/ 严格只读。

Exit codes：0 成功/空窗 · 2 窗参无效（畸形日历/倒序/未来窗）·
3 打包前对账失败（树计数不一致/媒体缺盘/名实不符）· 1 其他
（未知 guild/输出已存在未 --force/IO 错误）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # 直跑场景（cron/裸 python）补包根
    sys.path.insert(0, str(PROJECT_ROOT))

from src.archive.engine import (  # noqa: E402
    ReconciliationError,
    WindowError,
    plan_package,
    write_package,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create self-contained tar.zst packages from a time window"
        " of the entity tree (createTime in (from, to])",
    )
    parser.add_argument("--guild", required=True,
                        help="Guild data dir name under --data-root")
    parser.add_argument("--from", dest="from_ymd", required=True,
                        help="Window start, UTC YYYYMMDD (excluded — (from, to])")
    parser.add_argument("--to", dest="to_ymd", required=True,
                        help="Window end, UTC YYYYMMDD (included)")
    parser.add_argument("--apply", action="store_true",
                        help="Write the package (default: dry-run)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing output package")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "archives",
                        help="Archives root (default ./archives/;"
                             " package lands in {output}/{guild}/packages/)")
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "data",
                        help="Data root containing guild dirs (default ./data/)")
    parser.add_argument("--level", type=int, default=7,
                        help="zstd compression level (default %(default)s)")
    args = parser.parse_args(argv)

    try:
        plan = plan_package(args.data_root, args.guild, args.from_ymd, args.to_ymd)
        if plan.is_empty:
            print(f"no data in window ({args.from_ymd}, {args.to_ymd}]"
                  f" for guild {args.guild} — nothing to archive.")
            return 0
        counts = plan.counts
        print(f"window ({args.from_ymd}, {args.to_ymd}] guild {args.guild}:"
              f" feeds={counts['feeds']} comments={counts['comments']}"
              f" replies={counts['replies']} media={len(plan.media)}")
        if not args.apply:
            print("DRY RUN — no package written. Use --apply to create it.")
            return 0
        out_path = write_package(plan, args.output, level=args.level,
                                 force=args.force)
        print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes)")
        return 0
    except WindowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ReconciliationError as exc:
        print(f"ERROR: pre-pack reconciliation failed: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 —— CLI 兜底：未知 guild/IO/已存在等 → 1
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
