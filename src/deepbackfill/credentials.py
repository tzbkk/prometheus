"""deepbackfill 凭据面：conf 四键读写 + 掩码。

凭证唯一来源 = ptlogin2
网页扫码登录（weblogin.WebLoginClient），
complete() 产物经 CredentialStore 落 conf/deepbackfill.conf.json：

- 四键面：uin / p_uin / p_skey / minted_at。
  文件权限 0600；原子写（tmp + os.replace）。
- CredentialManager：AuthClient 的凭据源缝（credentials()）。remint()
  无本地重铸通道——可读错误指向扫码页（服务端
  AuthSessionManager 的 QR 会话负责重登录，auth.AuthClient 的重铸重试
  缝原样保留，收到本错误即收敛为可读 AuthError）。
- mask_secret：展示面掩码（首 4 末 4）——文件本体存全量（0600 秘密），
  日志/终端恒掩码。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CONF_ENV_VAR",
    "DEFAULT_CONF_PATH",
    "CredentialManager",
    "Credentials",
    "CredentialsError",
    "CredentialStore",
    "mask_secret",
]

logger = logging.getLogger(__name__)

DEFAULT_CONF_PATH = "conf/deepbackfill.conf.json"
CONF_ENV_VAR = "PROMETHEUS_DEEPBACKFILL_CONF"

_CONF_MODE = 0o600  # 凭证文件权限（含 p_skey——仅属主可读）


class CredentialsError(RuntimeError):
    """凭证面失败（conf 缺失/坏 conf/无重铸通道）——可读错误。"""


@dataclass(frozen=True)
class Credentials:
    """扫码登录产物（conf 四键的内存形）。

    uin 恒为纯数字串（cookie 值 o 前缀剥除）；p_uin 恒为 ``o<uin>`` 形
    ——auth 请求的 cookie 头按 §2.1 规格 ``p_uin=o<uin>; uin=<uin>;
    p_skey=<pskey>`` 组装。
    """

    uin: str
    p_uin: str
    p_skey: str
    minted_at: int


def mask_secret(secret: str) -> str:
    """掩码显示：首 4 末 4，中间以 … 截断（短串全掩）。"""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}…{secret[-4:]}"


class CredentialStore:
    """conf/deepbackfill.conf.json 的读写居所（0600 + 原子写）。

    读侧宽容缺文件（None——尚未扫码登录过）；坏 JSON/缺键 =
    CredentialsError（fail loud，禁多路回退）。save 后 chmod 0600（含
    首建——umask 无关，显式收紧）。生产写入唯一入口 = 服务端扫码成功面
    （AuthSessionManager：complete → 活测 → save）。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> Credentials | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialsError(f"{self.path} is unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise CredentialsError(f"{self.path} must be a JSON object")
        required = ("uin", "p_uin", "p_skey", "minted_at")
        for key in required:
            if key not in raw:
                raise CredentialsError(f"{self.path} lacks required key {key!r}")
        return Credentials(
            uin=str(raw["uin"]),
            p_uin=str(raw["p_uin"]),
            p_skey=str(raw["p_skey"]),
            minted_at=int(raw["minted_at"]),
        )

    def save(self, creds: Credentials) -> Path:
        """原子写（tmp + os.replace）+ 0600。"""
        doc = {
            "uin": creds.uin,
            "p_uin": creds.p_uin,
            "p_skey": creds.p_skey,
            "minted_at": creds.minted_at,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f"{self.path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.chmod(tmp, _CONF_MODE)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.chmod(self.path, _CONF_MODE)  # replace 后显式再钉（umask 无关）
        logger.info(
            "credentials saved to %s (p_skey %s, minted_at=%d)",
            self.path,
            mask_secret(creds.p_skey),
            creds.minted_at,
        )
        return self.path


class CredentialManager:
    """AuthClient 的凭据源（credentials() 缝的实现）。

    进程内缓存首载 conf。纯网路线下 remint() 无本地通道——重登录归
    服务端 QR 会话（trigger 时活测缺/失效自动起会话），本方法保留缝
    形状、恒抛可读错误。
    """

    def __init__(self, store: CredentialStore):
        self._store = store
        self._cache: Credentials | None = None
        self._lock = threading.Lock()

    def credentials(self) -> Credentials:
        with self._lock:
            if self._cache is None:
                self._cache = self._store.load()
            if self._cache is None:
                raise CredentialsError(
                    f"no deepbackfill credentials at {self._store.path} — "
                    "scan the QR code (start deepbackfill opens the browser; "
                    "the page lives at /auth/page)"
                )
            return self._cache

    def remint(self) -> Credentials:
        raise CredentialsError(
            "no local remint path on the pure-web route — re-scan the QR code "
            "(service starts a session automatically; page at /auth/page)"
        )
