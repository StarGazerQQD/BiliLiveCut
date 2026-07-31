"""P0 测试: 弹幕基线计算 + 突增评分(V0.1.6)。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import pytest

from app.analysis.highlight import (
    danmaku_rate_score,
    fuse_scores,
    weighted_rule_score,
)
from app.sources.bilibili.client import BilibiliRateLimitError, DanmakuServer, HttpErrorType
from app.sources.bilibili.danmaku import (
    OP_AUTH,
    OP_AUTH_REPLY,
    DanmakuAccess,
    DanmakuClient,
    DanmakuProtocolError,
    _iter_raw,
    encode_packet,
    server_endpoints,
)

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


@pytest.mark.asyncio
async def test_logged_error_immediately_falls_back_to_anonymous(monkeypatch: MonkeyPatch) -> None:
    """登录 token 请求返回业务错误后，应立即用无 Cookie 请求匿名 token。"""
    client = DanmakuClient(
        room_id=23771139,
        session_id=4,
        cookie="SESSDATA=valid; bili_jct=csrf; DedeUserID=123",
        login_retry_max_attempts=5,
        login_retry_interval_s=30,
    )
    attempts: list[str] = []
    anonymous = DanmakuServer(token="anonymous-token", hosts=[])

    async def fetch_server(cookie: str) -> DanmakuServer:
        attempts.append(cookie)
        if cookie:
            raise BilibiliRateLimitError(
                HttpErrorType.RISK_CONTROL,
                "接口触发平台风控, code=-352 message='-352'",
            )
        return anonymous

    monkeypatch.setattr("app.sources.bilibili.danmaku.time.monotonic", lambda: 100.0)
    monkeypatch.setattr(client, "_fetch_server", fetch_server)

    access = await client._select_access()  # noqa: SLF001

    assert attempts == ["SESSDATA=valid; bili_jct=csrf; DedeUserID=123", ""]
    assert access == DanmakuAccess(server=anonymous, uid=0, uses_cookie=False)
    assert client._login_failures == 1  # noqa: SLF001
    assert client.login_disabled is False
    assert client._next_login_retry_at == 130.0  # noqa: SLF001


@pytest.mark.asyncio
async def test_logged_access_uses_cookie_and_cookie_uid(monkeypatch: MonkeyPatch) -> None:
    """登录请求成功时应保留 Cookie 模式并在鉴权中使用其 UID。"""
    client = DanmakuClient(
        room_id=23771139,
        session_id=4,
        cookie="SESSDATA=valid; DedeUserID=456",
    )
    attempts: list[str] = []
    logged = DanmakuServer(token="logged-token", hosts=[])

    async def fetch_server(cookie: str) -> DanmakuServer:
        attempts.append(cookie)
        return logged

    monkeypatch.setattr(client, "_fetch_server", fetch_server)

    access = await client._select_access()  # noqa: SLF001

    assert attempts == ["SESSDATA=valid; DedeUserID=456"]
    assert access == DanmakuAccess(server=logged, uid=456, uses_cookie=True)


@pytest.mark.asyncio
async def test_anonymous_connection_stays_active_during_timed_login_retry(monkeypatch: MonkeyPatch) -> None:
    """匿名连接应持续工作，到达重试时间且登录恢复后才切换。"""
    client = DanmakuClient(
        room_id=23771139,
        session_id=4,
        cookie="SESSDATA=valid; DedeUserID=456",
        login_retry_max_attempts=5,
        login_retry_interval_s=0.01,
    )
    client._login_failures = 1  # noqa: SLF001
    client._next_login_retry_at = time.monotonic()  # noqa: SLF001
    anonymous = DanmakuAccess(DanmakuServer("anonymous", []), uid=0, uses_cookie=False)
    logged = DanmakuAccess(DanmakuServer("logged", []), uid=456, uses_cookie=True)
    anonymous_started = asyncio.Event()
    anonymous_cancelled = False

    async def consume(access: DanmakuAccess) -> None:
        nonlocal anonymous_cancelled
        assert access is anonymous
        anonymous_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            anonymous_cancelled = True
            raise

    async def retry_logged() -> DanmakuAccess:
        await anonymous_started.wait()
        return logged

    monkeypatch.setattr(client, "_connect_and_consume", consume)
    monkeypatch.setattr(client, "_try_logged_access", retry_logged)

    replacement = await asyncio.wait_for(client._consume_access(anonymous), timeout=1)  # noqa: SLF001

    assert replacement is logged
    assert anonymous_cancelled is True


@pytest.mark.asyncio
async def test_login_failure_limit_disables_cookie_for_current_recording(monkeypatch: MonkeyPatch) -> None:
    """达到配置上限后，本场后续 token 请求只能匿名。"""
    client = DanmakuClient(
        room_id=23771139,
        session_id=4,
        cookie="SESSDATA=valid; DedeUserID=456",
        login_retry_max_attempts=5,
        login_retry_interval_s=1,
    )
    for _index in range(5):
        client._record_login_failure(RuntimeError("errcode"))  # noqa: SLF001
    attempts: list[str] = []
    anonymous = DanmakuServer(token="anonymous", hosts=[])

    async def fetch_server(cookie: str) -> DanmakuServer:
        attempts.append(cookie)
        return anonymous

    monkeypatch.setattr(client, "_fetch_server", fetch_server)

    access = await client._select_access()  # noqa: SLF001

    assert client.login_disabled is True
    assert attempts == [""]
    assert access.uses_cookie is False


@pytest.mark.asyncio
async def test_login_auth_errcode_switches_run_loop_to_anonymous(monkeypatch: MonkeyPatch) -> None:
    """登录 WebSocket 返回错误码后，主循环应立即改用匿名连接。"""
    client = DanmakuClient(
        room_id=23771139,
        session_id=4,
        cookie="SESSDATA=valid; DedeUserID=456",
        login_retry_max_attempts=5,
        login_retry_interval_s=30,
    )
    logged = DanmakuAccess(DanmakuServer("logged", []), uid=456, uses_cookie=True)
    anonymous = DanmakuAccess(DanmakuServer("anonymous", []), uid=0, uses_cookie=False)
    selections = [logged, anonymous]
    consumed: list[bool] = []

    async def select_access() -> DanmakuAccess:
        return selections.pop(0)

    async def consume_access(access: DanmakuAccess) -> DanmakuAccess | None:
        consumed.append(access.uses_cookie)
        if access.uses_cookie:
            raise DanmakuProtocolError("弹幕鉴权被拒绝 code=-101")
        client.stop()
        return None

    monkeypatch.setattr(client, "_select_access", select_access)
    monkeypatch.setattr(client, "_consume_access", consume_access)

    await client.run()

    assert consumed == [True, False]
    assert client._login_failures == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_disabled_login_does_not_schedule_more_retry_tasks(monkeypatch: MonkeyPatch) -> None:
    """达到失败上限后，匿名连接不应再启动登录重试定时器。"""
    client = DanmakuClient(
        room_id=23771139,
        session_id=4,
        cookie="SESSDATA=valid; DedeUserID=456",
        login_retry_max_attempts=1,
        login_retry_interval_s=1,
    )
    client._record_login_failure(RuntimeError("errcode"))  # noqa: SLF001
    anonymous = DanmakuAccess(DanmakuServer("anonymous", []), uid=0, uses_cookie=False)
    consumed: list[DanmakuAccess] = []

    async def consume(access: DanmakuAccess) -> None:
        consumed.append(access)

    async def unexpected_retry() -> DanmakuAccess | None:
        raise AssertionError("登录已熔断，不应继续安排重试")

    monkeypatch.setattr(client, "_connect_and_consume", consume)
    monkeypatch.setattr(client, "_try_logged_access", unexpected_retry)

    replacement = await client._consume_access(anonymous)  # noqa: SLF001

    assert replacement is None
    assert consumed == [anonymous]


class _FakeWebSocket:
    """鉴权单测使用的最小 WebSocket。"""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.sent: list[bytes] = []

    async def send(self, message: bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> bytes:
        return self.reply


@pytest.mark.asyncio
async def test_authentication_packet_is_anonymous_and_reply_is_verified() -> None:
    """鉴权包必须固定 uid=0，并等待服务端 code=0 后才算连接成功。"""
    client = DanmakuClient(room_id=856077, session_id=4)
    ws = _FakeWebSocket(encode_packet(OP_AUTH_REPLY, b'{"code":0}'))

    await client._authenticate(ws, "ephemeral-token")  # noqa: SLF001

    assert len(ws.sent) == 1
    packets = list(_iter_raw(ws.sent[0]))
    assert len(packets) == 1
    _version, operation, body = packets[0]
    assert operation == OP_AUTH
    assert json.loads(body) == {
        "uid": 0,
        "roomid": 856077,
        "protover": 3,
        "platform": "web",
        "type": 2,
        "key": "ephemeral-token",
    }


@pytest.mark.asyncio
async def test_authentication_packet_uses_logged_uid() -> None:
    """登录连接的鉴权包应使用 Cookie 中解析出的 UID。"""
    client = DanmakuClient(room_id=856077, session_id=4, cookie="SESSDATA=valid; DedeUserID=789")
    ws = _FakeWebSocket(encode_packet(OP_AUTH_REPLY, b'{"code":0}'))

    await client._authenticate(ws, "logged-token", uid=789)  # noqa: SLF001

    _version, operation, body = list(_iter_raw(ws.sent[0]))[0]
    assert operation == OP_AUTH
    assert json.loads(body)["uid"] == 789


@pytest.mark.asyncio
async def test_authentication_rejection_raises_protocol_error() -> None:
    """服务端非零鉴权码必须阻止进入消息消费循环。"""
    client = DanmakuClient(room_id=856077, session_id=4)
    ws = _FakeWebSocket(encode_packet(OP_AUTH_REPLY, b'{"code":-101}'))

    with pytest.raises(DanmakuProtocolError, match="code=-101"):
        await client._authenticate(ws, "ephemeral-token")  # noqa: SLF001


def test_server_endpoints_deduplicates_and_validates_candidates() -> None:
    """候选节点应保持顺序、去重并忽略无效端口。"""
    hosts = [
        {"host": "a.example", "wss_port": 2245},
        {"host": "a.example", "wss_port": 2245},
        {"host": "b.example", "wss_port": "443"},
        {"host": "", "wss_port": 2245},
        {"host": "bad.example", "wss_port": 70000},
    ]

    assert server_endpoints(hosts) == [("a.example", 2245), ("b.example", 443)]
    assert server_endpoints([]) == [("broadcastlv.chat.bilibili.com", 2245)]


@pytest.mark.asyncio
async def test_candidate_node_failure_falls_back_to_next(monkeypatch: MonkeyPatch) -> None:
    """首节点网络失败时应使用同一 token 尝试下一候选节点。"""
    client = DanmakuClient(room_id=856077, session_id=4)
    attempts: list[tuple[str, int, str]] = []

    async def consume(host: str, port: int, access: DanmakuAccess) -> None:
        attempts.append((host, port, access.server.token))
        if host == "first.example":
            raise OSError("unreachable")

    monkeypatch.setattr(client, "_consume_endpoint", consume)
    await client._connect_and_consume(  # noqa: SLF001
        DanmakuAccess(
            server=DanmakuServer(
                token="ephemeral-token",
                hosts=[
                    {"host": "first.example", "wss_port": 2245},
                    {"host": "second.example", "wss_port": 2245},
                ],
            ),
            uid=0,
            uses_cookie=False,
        )
    )

    assert attempts == [
        ("first.example", 2245, "ephemeral-token"),
        ("second.example", 2245, "ephemeral-token"),
    ]


class TestDanmakuRateScore:
    """Sigmoid 映射评分测试。"""

    def test_high_ratio_tends_to_one(self) -> None:
        """高倍数时趋于 1.0:sigmoid(x=3.2 → 1/(1+e^(-3.2*1.6))。"""
        s = danmaku_rate_score(window_rate=100.0, baseline_rate=2.0, window_count=50)
        # ratio=50, sigmoid=~1.0
        assert s >= 0.99

    def test_equal_rate_low_score(self) -> None:
        """无突增时分数较低。"""
        s = danmaku_rate_score(window_rate=2.0, baseline_rate=2.0, window_count=50)
        # ratio=1, sigmoid(1)=1/(1+e^(-(1-1.8)*1.6) ≈ 1/(1+e^(1.28)) ≈ 0.22
        assert 0.0 <= s <= 0.4

    def test_moderate_spike(self) -> None:
        """3 倍突增应得中等分数。"""
        s = danmaku_rate_score(window_rate=9.0, baseline_rate=3.0, window_count=50)
        # ratio=3, sigmoid(3)=1/(1+e^(-(3-1.8)*1.6)) ≈ 1/(1+e^(-1.92)) ≈ 0.87
        assert 0.6 <= s <= 1.0

    def test_zero_baseline_protection(self) -> None:
        """基线为 0 时使用保护值,不除零。"""
        s = danmaku_rate_score(window_rate=10.0, baseline_rate=0.0, window_count=50)
        assert s == 0.35

    def test_low_volume_returns_zero(self) -> None:
        """低于 min_samples 时返回 0,不放大噪声。"""
        s = danmaku_rate_score(window_rate=0.5, baseline_rate=0.1, window_count=3)
        assert s == 0.0

    def test_window_count_zero_returns_zero(self) -> None:
        """无弹幕时返回 0。"""
        s = danmaku_rate_score(window_rate=0.0, baseline_rate=1.0, window_count=0)
        assert s == 0.0


class TestFuseScores:
    """信任策略融合函数。"""

    def test_close_scores_trust_rule(self) -> None:
        """规则与 LLM 分数接近时偏规则(alpha > beta)。"""
        s = fuse_scores(rule=0.8, llm_score=0.78, alpha=0.6, beta=0.4)
        assert 0.75 <= s <= 0.85

    def test_llm_influence(self) -> None:
        """LLM 有非零 beta 时影响结果。"""
        s = fuse_scores(rule=0.3, llm_score=0.9, alpha=0.5, beta=0.5)
        # (0.5*0.3 + 0.5*0.9) / 1.0 = 0.6
        assert 0.5 <= s <= 0.7

    def test_none_llm(self) -> None:
        """无 LLM 时直接返回规则分。"""
        s = fuse_scores(rule=0.65, llm_score=None, alpha=0.6, beta=0.0)
        assert s == 0.65

    def test_beta_zero_ignores_llm(self) -> None:
        """beta=0 时 LLM 不参与融合。"""
        s = fuse_scores(rule=0.3, llm_score=0.9, alpha=0.6, beta=0.0)
        # (0.6*0.3 + 0*0.9) / 0.6 = 0.3
        assert s == 0.3


class TestWeightedRuleScore:
    """多维加权评分。"""

    def test_all_zero(self) -> None:
        """全部零特征→零评分。"""
        s = weighted_rule_score({"a": 0.0, "b": 0.0}, {"a": 0.5, "b": 0.5})
        assert s == 0.0

    def test_missing_weight_is_zero(self) -> None:
        """不在权重字典中的特征不计入。"""
        s = weighted_rule_score({"a": 0.8, "b": 0.5}, {"a": 0.6})
        # 0.6*0.8 = 0.48, sum_w=0.6 → 0.48/0.6=0.8 (b 不计入权重和)
        assert s == 0.8

    def test_normal_distribution(self) -> None:
        """正常权重分配。"""
        s = weighted_rule_score({"a": 0.9, "b": 0.3}, {"a": 0.7, "b": 0.3})
        assert 0.6 <= s <= 1.0
