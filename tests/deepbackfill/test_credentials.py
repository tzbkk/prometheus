"""MD-100：conf 四键存储卫生 + 掩码 + 无本地重铸通道。"""

from __future__ import annotations

import json
import stat

import pytest

from src.deepbackfill.credentials import (
    Credentials,
    CredentialManager,
    CredentialsError,
    CredentialStore,
    mask_secret,
)

from tests.deepbackfill.conftest import P_SKEY


def test_credential_store_roundtrip_permissions_and_masking(tmp_path):
    conf = tmp_path / "deepbackfill.conf.json"
    store = CredentialStore(conf)

    assert store.load() is None  # 缺文件宽容（尚未扫码登录过）

    creds = Credentials(
        uin="10001",
        p_uin="o10001",
        p_skey=P_SKEY,
        minted_at=1782919600000,
    )
    store.save(creds)
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))  # 原子写零残留

    loaded = store.load()
    assert loaded == creds

    doc = json.loads(conf.read_text(encoding="utf-8"))
    assert set(doc) == {"uin", "p_uin", "p_skey", "minted_at"}  # 四键面
    assert doc["p_skey"] == P_SKEY  # 文件本体存全量（0600 秘密），掩码只在展示面

    assert mask_secret(P_SKEY) == "@XyZ…3456"
    assert mask_secret("short") == "****"

    # 凭据源缝：缺失 = 可读错误（指向扫码页）；纯网路线 remint 无本地通道。
    missing = CredentialManager(CredentialStore(tmp_path / "absent.json"))
    with pytest.raises(CredentialsError, match="scan the QR code"):
        missing.credentials()
    with pytest.raises(CredentialsError, match="pure-web route"):
        CredentialManager(store).remint()
    assert CredentialManager(store).credentials() == creds  # 在盘即返回

    # 坏 conf → fail loud（放在最后——覆写 conf）。
    conf.write_text("{broken", encoding="utf-8")
    with pytest.raises(CredentialsError, match="unreadable"):
        CredentialStore(conf).load()
    conf.write_text(json.dumps({"uin": "10001"}), encoding="utf-8")
    with pytest.raises(CredentialsError, match="lacks required key"):
        CredentialStore(conf).load()
