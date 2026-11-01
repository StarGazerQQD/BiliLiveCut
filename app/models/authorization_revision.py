# ruff: noqa: D100
from __future__ import annotations

import hmac
import json

from sqlmodel import Session

from app.core.settings_store import get_setting
from app.db.entities import AppSetting

AUTHORIZATION_REVISIONS_KEY = "model_authorization_revisions"
ConnectionAuthorization = tuple[str, str, bool]


def authorization_revisions(db: Session | None = None) -> dict[str, int]:
    """读取同一任务快照或指定事务中的非秘密授权变更序号。"""
    if db is None:
        raw = get_setting(AUTHORIZATION_REVISIONS_KEY, "{}")
    else:
        row = db.get(AppSetting, AUTHORIZATION_REVISIONS_KEY)
        raw = row.value if row is not None else "{}"
    value = json.loads(raw)
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or type(revision) is not int or revision < 0 for key, revision in value.items()
    ):
        raise ValueError("模型授权变更记录无效")
    return value


def update_authorization_revisions(
    db: Session,
    previous: dict[str, ConnectionAuthorization],
    current: dict[str, ConnectionAuthorization],
    revision: int,
) -> None:
    """仅连接存在性、端点、密钥或启用状态变化时更新；保留已删除连接的序号。"""
    revisions = authorization_revisions(db)
    changed = False
    for identity in previous.keys() | current.keys():
        before, after = previous.get(identity), current.get(identity)
        same = (
            before is not None
            and after is not None
            and (
                before[0].strip().rstrip("/") == after[0].strip().rstrip("/")
                and hmac.compare_digest(before[1].encode(), after[1].encode())
                and before[2] == after[2]
            )
        )
        if not same:
            revisions[identity] = revision
            changed = True
    if changed:
        row = db.get(AppSetting, AUTHORIZATION_REVISIONS_KEY) or AppSetting(key=AUTHORIZATION_REVISIONS_KEY, value="{}")
        row.value = json.dumps(revisions, sort_keys=True, separators=(",", ":"))
        db.add(row)
