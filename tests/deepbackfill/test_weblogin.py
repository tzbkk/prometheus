"""MD-116：weblogin 四步 + 码表 + 65 换码 + hash33 向量。"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from src.deepbackfill.auth import bkn
from src.deepbackfill.weblogin import (
    PTQRLOGIN_URL,
    PTQRSHOW_URL,
    QRState,
    WebLoginClient,
    WebLoginError,
    XLOGIN_URL,
    hash33,
)

from tests.deepbackfill.conftest import (
    CHECK_URL,
    NICKNAME,
    P_SKEY,
    QRSIG,
    TINY_PNG,
    UIN,
    FakeResponse,
    Headers,
    RecordingTransport,
    redirect_error,
)

T_MS = 1782919600123


def test_weblogin_four_step_code_table_and_hash33_vectors():
    # hash33 向量（先自验）：init=5381 与 auth.bkn 同函数（MD-101 同款向量）；
    # init=0 为 ptqrtoken 面。任务交叉向量 1739130872 为外部实现按 skey 算的
    # qzone 值，任何 DJB 变体不可复现（MD-101 预检，不采用）。
    assert hash33("", 0) == 0
    assert hash33("A", 0) == 65
    assert hash33("@AbCdEfGh1", 5381) == 1358553434
    assert hash33("@AbCdEfGh1", 5381) == bkn("@AbCdEfGh1")  # 同函数复用（勿重复造）
    assert 0 <= hash33(QRSIG, 0) < 0x80000000

    # 四步全流：xlogin 种子 → ptqrshow PNG → ptqrlogin 66/67/0 → complete 302。
    transport = RecordingTransport(
        {
            "xlogin": FakeResponse(
                b"<html></html>", headers=Headers(["pt_login_sig=SEED1; Path=/"])
            ),
            "ptqrshow": [
                FakeResponse(
                    TINY_PNG, headers=Headers([f"qrsig={QRSIG}; Path=/"])
                ),
                # 65 换码后的第二枚（qrsig 不同 → ptqrtoken 不同）
                FakeResponse(
                    TINY_PNG, headers=Headers(["qrsig=qr-sig-SECOND00000000; Path=/"])
                ),
            ],
            "ptqrlogin": [
                FakeResponse(
                    "ptuiCB('66','0','','0','二维码未扫描。','')".encode()
                ),
                FakeResponse(
                    f"ptuiCB('67','0','','0','二维码认证中。','{NICKNAME}')".encode()
                ),
                FakeResponse(
                    "ptuiCB('65','0','','0','二维码已失效。','')".encode()
                ),
                FakeResponse(
                    f"ptuiCB('0','0','{CHECK_URL}','0','登录成功！','{NICKNAME}', '')"
                    .encode()
                ),
            ],
            "check_sig": redirect_error(
                CHECK_URL,
                [f"p_skey={P_SKEY}; Path=/; Domain=pd.qq.com",
                 f"p_uin=o{UIN}; Path=/", f"uin=o{UIN}; Path=/"],
            ),
        }
    )
    client = WebLoginClient(transport=transport, now_ms=lambda: T_MS)

    png, qrsig = client.fetch_qr()
    assert png == TINY_PNG and qrsig == QRSIG

    waiting = client.poll_once(qrsig)
    assert waiting.state is QRState.WAITING and waiting.code == 66
    scanned = client.poll_once(qrsig)
    assert scanned.state is QRState.SCANNED and scanned.nickname == NICKNAME

    expired = client.poll_once(qrsig)
    assert expired.state is QRState.EXPIRED  # 65 → 调用方重跑 fetch_qr 换新码
    png2, qrsig2 = client.fetch_qr()
    assert png2 == TINY_PNG and qrsig2 == "qr-sig-SECOND00000000"

    success = client.poll_once(qrsig2)
    assert success.state is QRState.SUCCESS and success.check_url == CHECK_URL
    creds = client.complete(success.check_url)
    assert creds == {"p_skey": P_SKEY, "p_uin": f"o{UIN}", "uin": str(UIN)}  # o 前缀剥除

    # §1/§2 请求规格：xlogin 四参 + s_url；ptqrshow 参数族 + Referer；自动种子。
    xlogin_req = transport.requests[0]
    assert xlogin_req.full_url.startswith(XLOGIN_URL)
    assert parse_qs(urlparse(xlogin_req.full_url).query) == {
        "appid": ["1600001587"], "daid": ["823"], "style": ["8"],
        "hide_close_icon": ["1"], "s_url": ["https://pd.qq.com/explore"],
    }
    qr_req = transport.requests[1]
    assert qr_req.full_url.startswith(PTQRSHOW_URL)
    show_qs = parse_qs(urlparse(qr_req.full_url).query)
    assert show_qs["appid"] == ["1600001587"] and show_qs["daid"] == ["823"]
    assert show_qs["e"] == ["2"] and show_qs["l"] == ["M"] and show_qs["s"] == ["3"]
    assert show_qs["d"] == ["72"] and show_qs["v"] == ["4"] and show_qs["pt_3rd_aid"] == ["0"]
    assert "t" in show_qs  # random
    assert qr_req.get_header("Referer") == "https://xui.ptlogin2.qq.com/"

    # §3 ptqrlogin：ptqrtoken=hash33(qrsig,0)（换码后随新 qrsig 变）、action
    # epoch ms、Cookie 带 qrsig；宽正则吃 6~8 参不定形（上面 0 码样本为 7 参）。
    poll_reqs = [r for r in transport.requests if PTQRLOGIN_URL in r.full_url]
    assert len(poll_reqs) == 4
    tokens = [
        parse_qs(urlparse(r.full_url).query)["ptqrtoken"][0] for r in poll_reqs
    ]
    assert tokens[0] == str(hash33(QRSIG, 0))
    assert tokens[3] == str(hash33("qr-sig-SECOND00000000", 0)) != tokens[0]
    assert parse_qs(urlparse(poll_reqs[0].full_url).query)["action"] == [f"0-0-{T_MS}"]
    assert parse_qs(urlparse(poll_reqs[0].full_url).query)["aid"] == ["1600001587"]
    assert QRSIG in poll_reqs[0].get_header("Cookie")

    # 负例：非 ptuiCB body / 成功缺 check_url / check_url 未落 p_skey → 可读错误。
    broken = WebLoginClient(
        transport=RecordingTransport(
            {"ptqrlogin": FakeResponse(b"window.cb('x')")}
        )
    )
    broken.cookies["qrsig"] = QRSIG
    with pytest.raises(WebLoginError, match="non-ptuiCB"):
        broken.poll_once(QRSIG)

    no_url = WebLoginClient(
        transport=RecordingTransport(
            {"ptqrlogin": FakeResponse(b"ptuiCB('0','0','','0','ok','')")}
        )
    )
    with pytest.raises(WebLoginError, match="without check_url"):
        no_url.poll_once(QRSIG)

    no_pskey = WebLoginClient(
        transport=RecordingTransport(
            {"check_sig": FakeResponse(b"", headers=Headers([f"uin=o{UIN}"]))}
        )
    )
    with pytest.raises(WebLoginError, match="did not set p_skey"):
        no_pskey.complete(CHECK_URL)


def test_weblogin_check_sig_multi_domain_pskey_empty_must_not_win():
    """MD-XX 同名多域 p_skey：真值在前空串殿后（真机首扫序）与反序，
    complete() 都必须拿到 .pd.qq.com 真值——空值只补缺不覆写。"""
    for order in ("real_first", "empty_first"):
        cookies = [
            f"p_skey={P_SKEY}; Path=/; Domain=pd.qq.com",
            "p_skey=; Path=/; Domain=.qq.com",
            f"p_uin=o{UIN}; Path=/",
            f"uin=o{UIN}; Path=/",
        ]
        if order == "empty_first":
            cookies[0], cookies[1] = cookies[1], cookies[0]
        client = WebLoginClient(
            transport=RecordingTransport(
                {"check_sig": redirect_error(CHECK_URL, cookies)}
            )
        )
        creds = client.complete(CHECK_URL)
        assert creds["p_skey"] == P_SKEY, order
        assert creds["uin"] == str(UIN) and creds["p_uin"] == f"o{UIN}"


def test_weblogin_check_sig_uin_derived_from_p_uin_when_bare_uin_absent():
    """MD-122：appid 1600001587 的 check_sig 不种裸 uin
    （cookie 面：p_uin/pt2gguin）——uin 从 p_uin 剥 o 前缀导出。"""
    client = WebLoginClient(
        transport=RecordingTransport(
            {
                "check_sig": redirect_error(
                    CHECK_URL,
                    [f"p_skey={P_SKEY}; Path=/; Domain=pd.qq.com",
                     "p_skey=; Path=/; Domain=.qq.com",
                     f"p_uin=o{UIN}; Path=/; Domain=pd.qq.com",
                     f"pt2gguin=o{UIN}; Path=/"],
                )
            }
        )
    )
    creds = client.complete(CHECK_URL)
    assert creds == {"p_skey": P_SKEY, "p_uin": f"o{UIN}", "uin": str(UIN)}
