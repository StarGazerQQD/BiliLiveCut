"""业务配置注册、整体验证和带版本检查的事务保存。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from sqlalchemy import text
from sqlmodel import Session, select

from app.analysis.scoring_config import ScoringOptions, scoring_defaults
from app.core.config import Settings, get_settings
from app.core.configuration_policy import BOOTSTRAP, configuration_policy
from app.core.runtime_settings import active_configuration_snapshots, process_configuration, publish_settings
from app.db.entities import AppSetting
from app.db.session import get_session

_LOCK = RLock()
_REVISION = "configuration_revision"
SECRETS = {
    "admin_password",
    "reviewer_accounts_json",
    "bilibili_cookie",
    "trend_api_key",
    "asr_remote_api_key",
    "dingtalk_webhook",
    "dingtalk_secret",
    "wecom_webhook",
    "lark_app_secret",
    "smtp_password",
    "database_url",
    "biliup_upload_cmd",
}


class RuntimeOptions(BaseModel):
    """保留已有运行时开关键名和默认值。"""

    model_config = ConfigDict(extra="forbid")
    biliup_enabled: bool = False
    auto_upload: bool = False
    trend_schedule_enabled: bool = False
    trend_schedule_start: str = "03:00"
    trend_schedule_end: str = "05:00"
    trend_schedule_interval_min: int = Field(default=30, ge=1, le=1440)
    threshold_learning_enabled: bool = True
    danmaku_sentiment_enabled: bool = True
    storage_cleanup_enabled: bool = False
    scoring_configuration: ScoringOptions = Field(default_factory=scoring_defaults)

    @model_validator(mode="after")
    def validate_times(self) -> RuntimeOptions:
        """采集时间必须为有效的本地 HH:MM。"""
        for key in ("trend_schedule_start", "trend_schedule_end"):
            value = getattr(self, key)
            if not re.fullmatch(r"\d{2}:\d{2}", value) or int(value[:2]) > 23 or int(value[3:]) > 59:
                raise ValueError(f"{key} 必须为有效 HH:MM")
        return self


class ConfigurationChange(BaseModel):
    """省略或空白密钥保留，clear 明确清空，reset 删除覆盖。"""

    model_config = ConfigDict(extra="forbid")
    values: dict[str, JsonValue] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)
    reset: list[str] = Field(default_factory=list)
    revision: int | None = Field(default=None, ge=0)


class ConfigurationConflict(ValueError):
    """配置被其他页面修改，调用者必须读取最新版本后重试。"""


def _known_keys() -> set[str]:
    return set(Settings.model_fields) | set(RuntimeOptions.model_fields)


def _decode(key: str, value: str) -> JsonValue:
    field = Settings.model_fields.get(key) or RuntimeOptions.model_fields[key]
    if isinstance(field.default, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _validated(stored: dict[str, str]) -> tuple[Settings, RuntimeOptions]:
    values = {
        key: _decode(key, value) for key, value in stored.items() if key in _known_keys() and key not in BOOTSTRAP
    }
    core = get_settings().model_dump()
    core.update({key: value for key, value in values.items() if key in Settings.model_fields})
    return Settings.model_validate(core), RuntimeOptions.model_validate(
        {key: value for key, value in values.items() if key in RuntimeOptions.model_fields}
    )


def _read_rows(db: Session | None = None) -> tuple[dict[str, str], int]:
    if db is None:
        with get_session() as session:
            return _read_rows(session)
    rows = db.exec(select(AppSetting).where(AppSetting.key.in_(_known_keys() | {_REVISION}))).all()
    values = {row.key: row.value for row in rows if row.key not in BOOTSTRAP}
    revision = int(values.pop(_REVISION, "0"))
    return values, revision


def reload_configuration() -> None:
    """在同一读取事务中准备所有覆盖，提交后原子发布。"""
    with _LOCK:
        with get_session() as db:
            db.connection().exec_driver_sql("BEGIN")
            stored, revision = _read_rows(db)
            if stored:
                core, runtime = _validated(stored)
            else:
                core, runtime = get_settings(), RuntimeOptions()
            overrides, extras = _publication(db, stored, core, runtime)
        publish_settings(overrides, extras, revision=revision)


def _publication(
    db: Session, stored: dict[str, str], core: Settings, runtime: RuntimeOptions
) -> tuple[dict[str, object], dict[str, str]]:
    """在当前事务中准备完整快照，提交后不再进行可能失败的数据库读取。"""
    from app.models.configuration import MODEL_CONFIGURATION_KEY, validate_asr_selection

    validate_asr_selection(db, core)
    overrides = {key: getattr(core, key) for key in stored if key in Settings.model_fields and key not in BOOTSTRAP}
    extras = {key: _encode(value) for key, value in runtime.model_dump().items()}
    extras.update({key: _encode(getattr(core, key)) for key in Settings.model_fields if key not in BOOTSTRAP})
    for row in db.exec(
        select(AppSetting).where(
            AppSetting.key.in_({"llm_providers", MODEL_CONFIGURATION_KEY}) | AppSetting.key.startswith("plugin.")
        )
    ).all():
        extras[row.key] = row.value
    extras.setdefault("llm_providers", "")
    extras.setdefault(MODEL_CONFIGURATION_KEY, "")
    return overrides, extras


def _encode(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)


@contextmanager
def configuration_transaction(revision: int | None = None) -> Iterator[tuple[Session, int]]:
    """领域配置在同一版本、写事务和发布锁内保存，失败不发布部分快照。"""
    with _LOCK:
        with get_session() as db:
            db.connection().execute(text("BEGIN IMMEDIATE"))
            _, current = _read_rows(db)
            if revision is not None and revision != current:
                raise ConfigurationConflict("配置已在其他页面修改，请重新加载后合并草稿。")
            yield db, current + 1
            db.flush()
            stored, _ = _read_rows(db)
            core, runtime = _validated(stored)
            overrides, extras = _publication(db, stored, core, runtime)
            version = db.get(AppSetting, _REVISION) or AppSetting(key=_REVISION, value="0")
            version.value = str(current + 1)
            db.add(version)
        publish_settings(overrides, extras, revision=current + 1)


def save_configuration(change: ConfigurationChange) -> dict[str, JsonValue]:
    """完整验证后在一个 SQLite 事务内保存，非法或冲突请求零写入。"""
    requested = set(change.values) | set(change.clear) | set(change.reset)
    unknown = requested - _known_keys()
    if unknown:
        raise ValueError("存在未注册的配置项")
    if any(not isinstance(value, str) for key, value in change.values.items() if key in SECRETS):
        raise ValueError("凭据必须为文本；清空请使用 clear 操作")
    if requested & BOOTSTRAP.keys():
        raise ValueError("请求包含只读部署配置")
    if len(change.clear) != len(set(change.clear)) or len(change.reset) != len(set(change.reset)):
        raise ValueError("操作列表不能重复")
    if (
        set(change.clear) - SECRETS
        or set(change.clear) & set(change.reset)
        or set(change.values) & (set(change.clear) | set(change.reset))
    ):
        raise ValueError("同一配置不能同时设置、清空或恢复；只有凭据支持清空")
    with _LOCK:
        with get_session() as db:
            db.connection().execute(text("BEGIN IMMEDIATE"))
            rows = {
                row.key: row
                for row in db.exec(select(AppSetting).where(AppSetting.key.in_(_known_keys() | {_REVISION}))).all()
            }
            revision = int(rows[_REVISION].value) if _REVISION in rows else 0
            if change.revision is not None and change.revision != revision:
                raise ConfigurationConflict("配置已在其他页面修改，请重新加载后合并草稿。")
            stored = {key: row.value for key, row in rows.items() if key != _REVISION}
            previous = dict(stored)
            changes = {
                key: value
                for key, value in change.values.items()
                if key not in SECRETS or (isinstance(value, str) and value.strip() and value != "********")
            }
            for key, value in changes.items():
                stored[key] = _encode(value)
            for key in change.clear:
                stored[key] = ""
            for key in change.reset:
                stored.pop(key, None)
            try:
                core, runtime = _validated(stored)
            except ValidationError as exc:
                # 不返回 ValidationError.input 或包含用户输入的 ValueError 文案。
                errors = [
                    {
                        "field": ".".join(map(str, error["loc"])) or "cross_fields",
                        "type": error["type"],
                        "message": error["msg"],
                    }
                    for error in exc.errors()
                ]
                raise ValueError(
                    "配置校验失败（类型、范围或关联约束）：" + json.dumps(errors, ensure_ascii=False)
                ) from None
            validated = core.model_dump() | runtime.model_dump()
            from app.notify.email_verification import SMTP_KEYS, invalidate_verification

            if requested & SMTP_KEYS:
                old_core, _ = _validated(previous)
                if any(getattr(old_core, key) != getattr(core, key) for key in SMTP_KEYS):
                    invalidate_verification(db)
            for key in set(changes) | set(change.clear):
                row = rows.get(key) or AppSetting(key=key, value="")
                row.value = _encode(validated[key])
                db.add(row)
            for key in change.reset:
                if key in rows:
                    db.delete(rows[key])
            version = rows.get(_REVISION) or AppSetting(key=_REVISION, value="0")
            version.value = str(revision + 1)
            db.add(version)
            overrides, extras = _publication(db, stored, core, runtime)
            response = _configuration_view(stored, revision + 1, core, runtime, projected=True)
        publish_settings(overrides, extras, revision=revision + 1)
        return response


def save_registered_settings(values: dict[str, str]) -> bool:
    """让 CLI 与登录入口的已注册设置共用验证、版本号和发布事务。"""
    registered = set(values) & _known_keys()
    if not registered:
        return False
    if registered != set(values):
        raise ValueError("已注册业务配置不能与其他存储键混合写入")
    clear = [key for key, value in values.items() if key in SECRETS and not value.strip()]
    save_configuration(
        ConfigurationChange(
            values={key: _decode(key, value) for key, value in values.items() if key not in clear}, clear=clear
        )
    )
    return True


def _group(key: str) -> str:
    if key == "scoring_configuration":
        return "highlights"
    for group, prefixes in {
        "asr": ("asr_", "whisper_"),
        "llm": ("llm_", "transcript_llm_"),
        "trends": ("trend_",),
        "highlights": ("highlight_", "hotspot_", "editorial_", "threshold_", "near_live_", "background_asr_"),
        "review": ("review_", "reviewer_"),
        "output": ("clip_",),
        "notifications": ("notify_", "smtp_", "dingtalk_", "wecom_", "lark_", "disk_alert_"),
        "publishing": ("upload", "auto_upload", "auto_publish", "biliup_", "title_max_", "desc_max_"),
        "storage": ("storage_", "database_", "min_free_", "raw_retention", "low_disk", "critical_disk"),
        "recording": (
            "recording_",
            "segment_",
            "stream_",
            "preferred_stream",
            "reconnect_",
            "live_",
            "room_metadata_",
            "danmaku_",
            "collect_danmaku",
            "schedule_",
            "auto_recover_",
        ),
    }.items():
        if key.startswith(prefixes):
            return group
    return "system"


def configuration_view() -> dict[str, JsonValue]:
    """返回完整注册表、有效值、来源和约束，敏感值永不回显。"""
    stored, revision = _read_rows()
    core, runtime = _validated(stored)
    return _configuration_view(stored, revision, core, runtime)


def _configuration_view(
    stored: dict[str, str], revision: int, core: Settings, runtime: RuntimeOptions, *, projected: bool = False
) -> dict[str, JsonValue]:
    """生成同一版本的脱敏视图；活动任务各自保留旧快照。"""
    from app.core.configuration_labels import LABELS

    effective = core.model_dump() | runtime.model_dump()
    baseline = get_settings().model_dump() | RuntimeOptions().model_dump()
    current, extras, current_revision = process_configuration(prospective=core if projected else None)
    current_runtime = (
        runtime
        if projected
        else RuntimeOptions.model_validate(
            {key: _decode(key, value) for key, value in extras.items() if key in RuntimeOptions.model_fields}
        )
    )
    current_values = current.model_dump() | current_runtime.model_dump()
    snapshots = active_configuration_snapshots()
    schemas = Settings.model_json_schema()["properties"] | RuntimeOptions.model_json_schema()["properties"]
    fields: list[JsonValue] = []
    for key, schema in schemas.items():
        secret = key in SECRETS
        group = _group(key)
        reason = BOOTSTRAP.get(key)
        policy = configuration_policy(key)
        room_default = policy.effect == "new_room"
        retained = {"task": 0, "recording": 0}
        if policy.mode == "safe_boundary" and not policy.current_read:
            for snapshot in snapshots:
                previous = (
                    getattr(snapshot.settings, key)
                    if key in Settings.model_fields
                    else _decode(key, snapshot.extras[key])
                    if key in snapshot.extras
                    else baseline[key]
                )
                if previous != effective[key]:
                    retained[snapshot.boundary] += 1
        pending_restart = policy.mode == "restart" and current_values[key] != effective[key]
        fields.append(
            {
                "key": key,
                "env": key.upper() if key in Settings.model_fields else None,
                "label": LABELS.get(key, schema.get("description") or key),
                "description": schema.get("description", ""),
                "group": group,
                "scope": "new_room_default" if room_default else "global",
                "type": "object" if key == "scoring_configuration" else schema.get("type", "string"),
                "schema": ScoringOptions.model_json_schema() if key == "scoring_configuration" else None,
                "enum": schema.get("enum"),
                "minimum": schema.get("minimum"),
                "maximum": schema.get("maximum"),
                "default": None if secret else schema.get("default", baseline[key]),
                "baseline": None if secret else baseline[key],
                "value": None if secret else effective[key],
                "saved": None if secret else effective[key],
                "effective": None if secret else current_values[key],
                "configured": bool(effective[key]) if secret else None,
                "effective_configured": bool(current_values[key]) if secret else None,
                "pending_restart": pending_restart,
                "pending_service": current_values[key] != effective[key],
                "pending": current_values[key] != effective[key] or any(retained.values()),
                "retained_snapshots": retained,
                "secret": secret,
                "overridden": key in stored,
                "source": "web"
                if key in stored
                else ("environment" if key in get_settings().model_fields_set else "default"),
                "editable": not bool(reason),
                "reason": reason,
                "effect": policy.effect,
                "apply_mode": policy.mode,
                "effect_description": policy.description,
                "persistence": "environment" if reason else "database",
                "unit": "秒"
                if key.endswith(("_s", "_seconds"))
                else "GB"
                if key.endswith("_gb")
                else "天"
                if key.endswith("_days")
                else None,
            }
        )
    return {
        "revision": revision,
        "runtime_revision": revision if projected else current_revision,
        "fields": fields,
        "scopes": [
            {"id": "rooms", "label": "房间配置与自动开播", "url": "/?tab=rooms", "persistence": "LiveRoom"},
            {
                "id": "providers",
                "label": "模型服务商与密钥",
                "url": "/?tab=models",
                "persistence": "database",
            },
            {"id": "account", "label": "Bilibili 登录与 Cookie", "url": "/?tab=login", "persistence": "database"},
            {"id": "plugins", "label": "插件配置", "url": "/?tab=plugins", "persistence": "database"},
            {
                "id": "launcher",
                "label": "Web 端口",
                "url": "/?tab=features",
                "persistence": "launcher.json",
                "effect": "restart",
            },
        ],
    }
