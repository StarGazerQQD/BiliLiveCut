"""Bilibili 网页端 WBI 查询签名。

直播网页会先从公开导航响应读取 ``wbi_img`` 的两段图片键，再用固定混排表
为查询参数生成短期有效的 ``wts`` / ``w_rid``。本模块只实现这一无状态、
可单测的网页请求格式，不读取 Cookie，也不生成或伪造浏览器指纹。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode, urlparse

_MIXIN_KEY_ENC_TAB = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)
_INVALID_VALUE_CHARS = re.compile(r"[!'()*]")
_WBI_IMAGE_KEY = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(frozen=True, slots=True)
class WbiKeys:
    """WBI 图片键。

    :param image: ``img_url`` 文件名中的 32 位键。
    :param sub: ``sub_url`` 文件名中的 32 位键。
    """

    image: str
    sub: str

    @classmethod
    def from_urls(cls, image_url: str, sub_url: str) -> WbiKeys:
        """从公开导航响应的图片 URL 提取 WBI 键。

        :param image_url: ``wbi_img.img_url``。
        :param sub_url: ``wbi_img.sub_url``。
        :returns: 校验后的键。
        :raises ValueError: URL 中缺少合法的 32 位十六进制文件名时。
        """
        image = PurePosixPath(urlparse(image_url).path).stem
        sub = PurePosixPath(urlparse(sub_url).path).stem
        if not _WBI_IMAGE_KEY.fullmatch(image) or not _WBI_IMAGE_KEY.fullmatch(sub):
            raise ValueError("WBI 图片 URL 中缺少合法键")
        return cls(image=image.lower(), sub=sub.lower())


def build_mixin_key(keys: WbiKeys) -> str:
    """按网页端混排表生成 32 位签名盐。

    :param keys: WBI 图片键。
    :returns: 32 位混排键。
    """
    raw = keys.image + keys.sub
    return "".join(raw[index] for index in _MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi_params(
    params: Mapping[str, object],
    keys: WbiKeys,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    """为查询参数附加网页端 ``wts`` 与 ``w_rid``。

    参数值会先删除网页算法排除的五个字符，再按键名排序并进行 URL 编码。
    返回新字典，不修改调用方数据。

    :param params: 待签名查询参数。
    :param keys: 当前 WBI 图片键。
    :param timestamp: 可选 Unix 秒时间戳；省略时使用当前时间。
    :returns: 包含 ``wts`` / ``w_rid`` 的字符串参数字典。
    """
    signed = {
        str(key): _INVALID_VALUE_CHARS.sub("", str(value))
        for key, value in params.items()
        if key not in {"w_rid", "wts"}
    }
    signed["wts"] = str(int(time.time()) if timestamp is None else int(timestamp))
    query = urlencode(sorted(signed.items()), quote_via=quote)
    signed["w_rid"] = hashlib.md5((query + build_mixin_key(keys)).encode()).hexdigest()
    return signed
