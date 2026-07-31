"""Bilibili 网页 WBI 签名回归测试。"""

from __future__ import annotations

import pytest

from app.sources.bilibili.wbi import WbiKeys, build_mixin_key, sign_wbi_params


def test_live_page_signature_matches_observed_anonymous_request() -> None:
    """固定网页样本应生成浏览器实际发送的 ``w_rid``。"""
    keys = WbiKeys.from_urls(
        "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
        "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
    )

    signed = sign_wbi_params(
        {"id": 856077, "type": 0, "web_location": "444.8"},
        keys,
        timestamp=1785418470,
    )

    assert build_mixin_key(keys) == "ea1db124af3c7062474693fa704f4ff8"
    assert signed == {
        "id": "856077",
        "type": "0",
        "web_location": "444.8",
        "wts": "1785418470",
        "w_rid": "562961130f76fde1d8af29d00ebff6a5",
    }


def test_signing_does_not_mutate_input_and_filters_disallowed_chars() -> None:
    """签名应复制参数并执行网页端字符过滤。"""
    params = {"keyword": "a!'()*b", "wts": "stale", "w_rid": "stale"}
    keys = WbiKeys(image="7cd084941338484aae1ad9425b84077c", sub="4932caff0ff746eab6f01bf08b70ac45")

    signed = sign_wbi_params(params, keys, timestamp=10)

    assert params == {"keyword": "a!'()*b", "wts": "stale", "w_rid": "stale"}
    assert signed["keyword"] == "ab"
    assert signed["wts"] == "10"
    assert len(signed["w_rid"]) == 32


@pytest.mark.parametrize(
    ("image_url", "sub_url"),
    [
        ("", "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"),
        ("https://example.com/not-a-key.png", "https://example.com/also-invalid.png"),
    ],
)
def test_invalid_wbi_urls_are_rejected(image_url: str, sub_url: str) -> None:
    """缺失或异常图片键不得生成签名。"""
    with pytest.raises(ValueError, match="WBI 图片 URL"):
        WbiKeys.from_urls(image_url, sub_url)
