"""archive 组件：时间窗打包引擎（批处理，非常驻）。

打包是批处理操作，不是服务：引擎 src/archive/engine.py（窗口数学/对账/
tar.zst 写出）+ CLI scripts/archive.py（手动/cron 唯一入口）+ Manifest 契约。
"""

from src.archive.engine import (
    MediaRef,
    PackagePlan,
    ReconciliationError,
    WindowError,
    build_manifest,
    list_guilds,
    list_packages,
    package_stem,
    plan_package,
    write_package,
)

__all__ = [
    "MediaRef",
    "PackagePlan",
    "ReconciliationError",
    "WindowError",
    "build_manifest",
    "list_guilds",
    "list_packages",
    "package_stem",
    "plan_package",
    "write_package",
]
