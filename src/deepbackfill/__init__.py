"""deepbackfill——全量采集服务（纯网路线）。

与 scraper 完全同化——数据面五键全同（Feed/Comment/
Reply/MediaAsset/ProcessLock 经 entity_store）、API 面同族（五端点），
合法差异仅凭据面（auth guild_feed_reader vs noauth commreader）与 feed
清单获取器（isFinish 全史翻页 vs 5.5 月窗口）。模块分工：

- weblogin：ptlogin2 纯网页扫码登录客户端（四步协议——二维码图片在
  浏览器里展示，零终端渲染）。
- credentials：conf 四键读写（0600）+ 掩码。
- auth：AuthClient（§2.1 请求规格逐字）+ bkn 自算（§2.2）。
- runner：全史回填编排（复用 web_scraper 组件——store/comments/媒体池）。
- service + __main__：:9424 五端点 + /auth/* 三端点（PNG 直出/状态机/
  内嵌页）+ AuthSessionManager（凭证探测懒启动，缺/失效自动起 QR 会话）。
"""

from src.deepbackfill.auth import AUTH_BASE_URL, AuthClient, AuthError, bkn
from src.deepbackfill.credentials import (
    CONF_ENV_VAR,
    DEFAULT_CONF_PATH,
    CredentialManager,
    Credentials,
    CredentialsError,
    CredentialStore,
    mask_secret,
)
from src.deepbackfill.weblogin import (
    QRState,
    WebLoginClient,
    WebLoginError,
    hash33,
)

__all__ = [
    "AUTH_BASE_URL",
    "CONF_ENV_VAR",
    "DEFAULT_CONF_PATH",
    "AuthClient",
    "AuthError",
    "CredentialManager",
    "Credentials",
    "CredentialsError",
    "CredentialStore",
    "QRState",
    "WebLoginClient",
    "WebLoginError",
    "bkn",
    "hash33",
    "mask_secret",
]
