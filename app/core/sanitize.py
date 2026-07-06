"""敏感信息脱敏工具。

对日志/__repr__/错误消息中的 Cookie、API Key、密码等敏感字段进行掩码处理,
防止通过日志或 Web 界面意外泄露凭据。
"""

from __future__ import annotations

import re

# ── 脱敏模式 ──────────────────────────────────────────────────────

_COOKIE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(SESSDATA=)[^;]*(;?)", re.IGNORECASE), r"\1***\2"),
    (re.compile(r"(bili_jct=)[^;]*(;?)", re.IGNORECASE), r"\1***\2"),
    (re.compile(r"(DedeUserID=)[^;]*(;?)", re.IGNORECASE), r"\1***\2"),
    (re.compile(r"(buvid3=)[^;]*(;?)", re.IGNORECASE), r"\1***\2"),
    (re.compile(r"(buvid4=)[^;]*(;?)", re.IGNORECASE), r"\1***\2"),
    (re.compile(r"(dedeuserid_ckmd5=)[^;]*(;?)", re.IGNORECASE), r"\1***\2"),
    (re.compile(r"(sid=)[^;]*(;?)", re.IGNORECASE), r"\1***\2"),
]

_API_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # key=... 或 KEY=... 或 api_key=... 等
    (re.compile(r"(\b(?:api[_-]?key|apikey|key)\s*=\s*)(\S+)", re.IGNORECASE), r"\1***"),
    # sk-... (OpenAI 风格的 API key)
    (re.compile(r"(sk-[a-zA-Z0-9]{4,})[\w-]*"), r"\1***"),
    # Bearer token / Authorization header
    (re.compile(r"(Authorization:\s*Bearer\s+)(\S+)", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Authorization:\s*Basic\s+)(\S+)", re.IGNORECASE), r"\1***"),
]

_PASSWORD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(\b(?:password|passwd|pwd|secret)\s*[:=]\s*)(\S+)", re.IGNORECASE), r"\1***"),
    (re.compile(r"(--(?:password|passwd|pwd|secret)\s+)(\S+)", re.IGNORECASE), r"\1***"),
]

_URL_TOKEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # token=<value> in URL query strings
    (re.compile(r"([?&](?:token|access_token|auth|auth_token)\s*=\s*)([^&\s]+)", re.IGNORECASE), r"\1***"),
    # access_key=<value> in URLs
    (re.compile(r"([?&](?:access_key|secret_key)\s*=\s*)([^&\s]+)", re.IGNORECASE), r"\1***"),
]


def sanitize_text(text: str) -> str:
    """对文本中的敏感信息进行脱敏处理。

    检测并掩码以下类型的敏感数据:

    * Cookie 值 (SESSDATA, bili_jct, DedeUserID 等)
    * API Key (key=..., sk-..., Authorization 头)
    * 密码字段 (password, passwd, pwd, secret)
    * URL 中的 token/access_key 参数

    :param text: 原始文本。
    :returns: 脱敏后的文本。
    """
    if not text:
        return text

    result = text

    for pattern, replacement in _COOKIE_PATTERNS:
        result = pattern.sub(replacement, result)

    for pattern, replacement in _API_KEY_PATTERNS:
        result = pattern.sub(replacement, result)

    for pattern, replacement in _PASSWORD_PATTERNS:
        result = pattern.sub(replacement, result)

    for pattern, replacement in _URL_TOKEN_PATTERNS:
        result = pattern.sub(replacement, result)

    return result


def sanitize_cookie(cookie_string: str) -> str:
    """专门对 Bilibili Cookie 字符串进行脱敏。

    保留 cookie key 名称, 仅掩码 value 部分。未识别的 cookie 键也进行通用掩码。

    :param cookie_string: 原始 Cookie 字符串 (如 ``key1=val1; key2=val2``)。
    :returns: 脱敏后的 Cookie 字符串。
    """
    if not cookie_string:
        return cookie_string
    parts = cookie_string.split(";")
    sanitized_parts = []
    for part in parts:
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            sanitized_parts.append(f"{key.strip()}=***")
        else:
            sanitized_parts.append(part)
    return "; ".join(sanitized_parts)
