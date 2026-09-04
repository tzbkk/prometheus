"""产品版本机制：演进期不建 tag，无 tag 环境回退 1.0.0-rc.1；
首个 tag（v1.0.0）由作者建立。
"""

from __future__ import annotations

import importlib
import subprocess
from unittest.mock import patch

import prometheus_version


def test_version_falls_back_to_rc_until_first_tag():
    """MD-137：无 tag 环境（describe 失败）→ 1.0.0-rc.1；describe 成功 → tag 接管。

    项目演进期不建 tag，回退值即对外版本；tag 出现后 describe
    优先，v 前缀剥除。
    """
    completed = subprocess.CompletedProcess([], 128, stdout="", stderr="")
    with patch.object(subprocess, "run", return_value=completed):
        module = importlib.reload(prometheus_version)
        assert module.__version__ == "1.0.0-rc.1"

    tagged = subprocess.CompletedProcess([], 0, stdout="v1.0.0\n", stderr="")
    with patch.object(subprocess, "run", return_value=tagged):
        module = importlib.reload(prometheus_version)
        assert module.__version__ == "1.0.0"

    importlib.reload(prometheus_version)
