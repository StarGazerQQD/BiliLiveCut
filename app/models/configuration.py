# ruff: noqa: D100
from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlmodel import Session

from app.core.cancellation import check_cancellation, check_shutdown
from app.core.runtime_settings import effective_settings
from app.core.settings_store import get_setting
from app.db.entities import AppSetting
from app.db.session import get_session
from app.models.capacity import MODEL_CAPACITIES
from app.models.contracts import (
    TEXT_CAPABILITIES,
    Capability,
    ConnectionView,
    ModelConfiguration,
    ModelConfigurationChange,
    ModelConfigurationView,
    ModelConnection,
    ModelProbeDiagnostic,
    ModelRoute,
)

if TYPE_CHECKING:
    from app.core.config import Settings

MODEL_CONFIGURATION_KEY = "model_configuration"
_PROBE_PREFIX = "model_probe:v1:"


def _stored(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row is not None else ""


def _parse(value: str) -> ModelConfiguration:
    try:
        return ModelConfiguration.model_validate_json(value)
    except ValidationError:
        raise ValueError("已保存的模型配置无效，请检查连接、路由引用及字段约束") from None


def configured_models() -> ModelConfiguration | None:
    """实际调用读取任务快照；尚未启用独立路由时保留原有业务配置。"""
    value = get_setting(MODEL_CONFIGURATION_KEY, "")
    return _parse(value) if value else None


class ModelAuthorizationError(RuntimeError):
    """当前连接已撤销或变更，旧执行上下文不得继续发起请求。"""


def authorize_model_call(connection_id: str, route_id: str, base_url: str, api_key: str) -> None:
    """请求发送前直读持久授权；模型参数仍由任务快照决定，不记录任何密钥。"""
    with get_session() as db:
        db.connection().exec_driver_sql("BEGIN")
        core, _ = _core(db)
        current = _read(db, core)
        connection = next((item for item in current.connections if item.id == connection_id), None)
        route = next((item for item in current.routes if item.id == route_id), None)
        if (
            connection is None
            or not connection.enabled
            or connection.base_url.rstrip("/") != base_url.rstrip("/")
            or not hmac.compare_digest(connection.api_key.encode(), api_key.encode())
            or route is None
            or not route.enabled
            or route.connection_id != connection_id
        ):
            raise ModelAuthorizationError("模型连接授权已撤销或变更，请使用当前配置重新发起操作")


def authorize_model_probe(revision: int) -> None:
    """草稿允许使用未保存的连接，但配置变更后不能继续测试或重试旧草稿。"""
    with get_session() as db:
        _, current = _core(db)
        if current != revision:
            raise ModelAuthorizationError("模型测试期间配置已变更，请重新加载后再测试")


def _authorize_direct_asr(base_url: str, api_key: str) -> None:
    # 未选择目录路由时，独立 ASR 配置仍是授权来源，与文字模型目录是否启用无关。
    with get_session() as db:
        db.connection().exec_driver_sql("BEGIN")
        core, _ = _core(db)
        if core.asr_remote_base_url.rstrip("/") != base_url.rstrip("/") or not hmac.compare_digest(
            core.asr_remote_api_key.encode(), api_key.encode()
        ):
            raise ModelAuthorizationError("远程 ASR 连接授权已撤销或变更，请使用当前配置重新发起操作")


def asr_call_authorizer(payload: dict[str, object]) -> Callable[[], None]:
    """授权仅在父进程核对，子进程每次上传音频前必须等待父进程许可。"""
    selected = effective_settings().asr_remote_route_id
    if not selected:
        return partial(_authorize_direct_asr, str(payload["asr_remote_base_url"]), str(payload["asr_remote_api_key"]))
    selection = _validate_selection(configured_models(), selected)
    assert selection is not None
    connection_id, route_id = selection[1].id, selection[0].id
    return partial(
        authorize_model_call,
        connection_id,
        route_id,
        str(payload["asr_remote_base_url"]),
        str(payload["asr_remote_api_key"]),
    )


def _legacy(db: Session, core: Settings) -> ModelConfiguration:
    from app.analysis.llm_providers import _coerce

    raw = _stored(db, "llm_providers")
    try:
        values = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        raise ValueError("llm_providers 不是有效 JSON") from None
    if not isinstance(values, list):
        raise ValueError("llm_providers 必须是数组")
    providers = [_coerce(value) for value in values]
    connections: list[ModelConnection] = []
    routes: list[ModelRoute] = []
    for provider in providers:
        connection_id = f"legacy-{provider.id}"
        connections.append(
            ModelConnection(
                id=connection_id,
                name=provider.name,
                base_url=provider.base_url,
                api_key=provider.api_key,
                enabled=provider.enabled and bool(provider.api_key),
            )
        )
        for capability in TEXT_CAPABILITIES:
            routes.append(
                ModelRoute(
                    id=f"{capability}-{provider.id}",
                    name=provider.name,
                    capability=capability,
                    connection_id=connection_id,
                    model=provider.model,
                    priority=provider.priority,
                    enabled=provider.enabled,
                    web_search_param=provider.web_search_param,
                    price_input_per_m=provider.price_input_per_m,
                    price_output_per_m=provider.price_output_per_m,
                )
            )
    if core.trend_api_key and core.trend_base_url and core.trend_model:
        connections.append(
            ModelConnection(
                id="dedicated-trends",
                name="原热点专用连接",
                base_url=core.trend_base_url,
                api_key=core.trend_api_key,
            )
        )
        routes.append(
            ModelRoute(
                id="dedicated-trends",
                name="原热点专用模型",
                connection_id="dedicated-trends",
                capability="trends",
                model=core.trend_model,
                web_search_param=core.trend_web_search_param,
                priority=min((item.priority for item in routes), default=100) - 1,
            )
        )
    if core.asr_remote_base_url and core.asr_remote_model:
        connections.append(
            ModelConnection(
                id="dedicated-asr",
                name="原远程转写连接",
                base_url=core.asr_remote_base_url,
                api_key=core.asr_remote_api_key,
            )
        )
        routes.append(
            ModelRoute(
                id="dedicated-asr",
                name="原远程转写模型",
                connection_id="dedicated-asr",
                capability="asr",
                model=core.asr_remote_model,
            )
        )
    return ModelConfiguration(connections=connections, routes=routes)


def _read(db: Session, core: Settings) -> ModelConfiguration:
    value = _stored(db, MODEL_CONFIGURATION_KEY)
    return _parse(value) if value else _legacy(db, core)


def _core(db: Session) -> tuple[Settings, int]:
    from app.core.configuration import _read_rows, _validated

    stored, revision = _read_rows(db)
    core, _ = _validated(stored)
    return core, revision


def _view(
    config: ModelConfiguration,
    core: Settings,
    revision: int,
    *,
    legacy: bool,
    diagnostics: list[ModelProbeDiagnostic] | None = None,
) -> ModelConfigurationView:
    enabled = {item.id for item in config.connections if item.enabled}
    return ModelConfigurationView(
        revision=revision,
        source="legacy" if legacy else "routes",
        asr_selection=core.asr_remote_route_id,
        model_capacities=list(MODEL_CAPACITIES),
        probe_diagnostics=diagnostics or [],
        connections=[
            ConnectionView(
                id=item.id,
                name=item.name,
                base_url=item.base_url,
                enabled=item.enabled,
                api_key_set=bool(item.api_key),
            )
            for item in config.connections
        ],
        routes=config.routes,
        active_counts={
            capability: sum(
                item.capability == capability and item.enabled and item.connection_id in enabled
                for item in config.routes
            )
            for capability in (*TEXT_CAPABILITIES, "asr")
        },
    )


def model_configuration_view() -> ModelConfigurationView:
    """管理界面始终读取最新持久化版本，兼容转换只读、不自动保存。"""
    with get_session() as db:
        db.connection().exec_driver_sql("BEGIN")
        core, revision = _core(db)
        config = _read(db, core)
        legacy = not bool(_stored(db, MODEL_CONFIGURATION_KEY))
        return _view(
            config, core, revision, legacy=legacy, diagnostics=[] if legacy else _probe_diagnostics(db, config)
        )


def _probe_diagnostics(db: Session, config: ModelConfiguration) -> list[ModelProbeDiagnostic]:
    result = []
    for route in config.routes:
        row = db.get(AppSetting, _PROBE_PREFIX + route.id)
        if row is not None:
            result.append(ModelProbeDiagnostic.model_validate_json(row.value))
    return result


def _same_probe_connection(first: ModelConnection | None, second: ModelConnection | None) -> bool:
    return (
        first is not None
        and second is not None
        and (
            first.id == second.id
            and first.base_url == second.base_url
            and first.enabled == second.enabled
            and hmac.compare_digest(first.api_key.encode(), second.api_key.encode())
        )
    )


def _invalidate_probe_diagnostics(db: Session, previous: ModelConfiguration, current: ModelConfiguration) -> None:
    old_connections = {item.id: item for item in previous.connections}
    new_connections = {item.id: item for item in current.connections}
    new_routes = {item.id: item for item in current.routes}
    ignored = {"name", "priority", "price_input_per_m", "price_output_per_m"}
    for route in previous.routes:
        new_route = new_routes.get(route.id)
        if (
            new_route is not None
            and route.model_dump(exclude=ignored) == new_route.model_dump(exclude=ignored)
            and _same_probe_connection(
                old_connections.get(route.connection_id), new_connections.get(new_route.connection_id)
            )
        ):
            continue
        row = db.get(AppSetting, _PROBE_PREFIX + route.id)
        if row is not None:
            db.delete(row)


def save_probe_diagnostic(route: ModelRoute, connection: ModelConnection, diagnostic: ModelProbeDiagnostic) -> bool:
    """只保存仍与已存配置一致的选中路由观察，草稿及失效请求不写入。"""
    check_cancellation()
    with get_session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        raw = _stored(db, MODEL_CONFIGURATION_KEY)
        if not raw:
            return False
        config = _parse(raw)
        if not _probe_matches(config, route, connection):
            return False
        if (diagnostic.route_id, diagnostic.connection_id, diagnostic.model, diagnostic.base_url) != (
            route.id,
            connection.id,
            route.model,
            connection.base_url,
        ):
            raise ValueError("模型探测诊断与选中路由不一致")
        check_shutdown()
        row = db.get(AppSetting, _PROBE_PREFIX + route.id) or AppSetting(key=_PROBE_PREFIX + route.id, value="")
        row.value = diagnostic.model_dump_json()
        db.add(row)
    return True


def _probe_matches(config: ModelConfiguration, route: ModelRoute, connection: ModelConnection) -> bool:
    current_route = next((item for item in config.routes if item.id == route.id), None)
    current_connection = next((item for item in config.connections if item.id == connection.id), None)
    return (
        current_route == route
        and current_connection is not None
        and current_connection.name == connection.name
        and _same_probe_connection(current_connection, connection)
    )


def probe_is_saved(route: ModelRoute, connection: ModelConnection) -> bool:
    """发送前确认该选中路由已保存；其他用户随后保存同名草稿不能改变此结论。"""
    with get_session() as db:
        raw = _stored(db, MODEL_CONFIGURATION_KEY)
        return bool(raw) and _probe_matches(_parse(raw), route, connection)


def _merge(change: ModelConfigurationChange, existing: ModelConfiguration) -> ModelConfiguration:
    previous = {item.id: item for item in existing.connections}
    connections: list[ModelConnection] = []
    for item in change.connections:
        old = previous.get(item.id)
        key = item.api_key
        if item.clear_api_key:
            if key and not key.startswith("****"):
                raise ValueError("同一连接不能同时设置和清空密钥")
            key = ""
        elif not key or key.startswith("****"):
            if old is not None and old.api_key and old.base_url != item.base_url:
                raise ValueError("修改服务地址时必须重新填写密钥或明确清空，防止将原凭据发送到新地址")
            key = old.api_key if old is not None else ""
        connections.append(
            ModelConnection.model_validate(item.model_dump(exclude={"clear_api_key"}) | {"api_key": key})
        )
    return ModelConfiguration(connections=connections, routes=change.routes)


def save_model_configuration(change: ModelConfigurationChange) -> ModelConfigurationView:
    """凭据合并、引用校验、版本递增及配置发布共用一个写事务。"""
    from app.core.configuration import configuration_transaction

    with configuration_transaction(change.revision) as (db, revision):
        core, _ = _core(db)
        previous = _read(db, core)
        merged = _merge(change, previous)
        _validate_selection(merged, core.asr_remote_route_id)
        _invalidate_probe_diagnostics(db, previous, merged)
        row = db.get(AppSetting, MODEL_CONFIGURATION_KEY) or AppSetting(key=MODEL_CONFIGURATION_KEY, value="")
        row.value = merged.model_dump_json()
        db.add(row)
        old = db.get(AppSetting, "llm_providers")
        if old is not None:
            db.delete(old)
        db.flush()
        response = _view(merged, core, revision, legacy=False, diagnostics=_probe_diagnostics(db, merged))
    return response


def probe_configuration(change: ModelConfigurationChange) -> ModelConfiguration:
    """测试草稿也使用最新凭据与版本检查，避免旧页面误测已轮换凭据。"""
    from app.core.configuration import ConfigurationConflict

    with get_session() as db:
        db.connection().exec_driver_sql("BEGIN")
        core, revision = _core(db)
        if revision != change.revision:
            raise ConfigurationConflict("配置已在其他页面修改，请重新加载后合并草稿。")
        return _merge(change, _read(db, core))


def resolve_routes(config: ModelConfiguration, capability: Capability) -> list[tuple[ModelRoute, ModelConnection]]:
    """仅返回目标业务启用的路由及其连接，顺序与其他业务互不影响。"""
    connections = {item.id: item for item in config.connections if item.enabled}
    return [
        (route, connections[route.connection_id])
        for route in sorted(config.routes, key=lambda x: (x.priority, x.name, x.id))
        if route.enabled and route.capability == capability and route.connection_id in connections
    ]


def _validate_selection(config: ModelConfiguration | None, selected: str) -> tuple[ModelRoute, ModelConnection] | None:
    if not selected:
        return None
    for route, connection in resolve_routes(config, "asr") if config is not None else []:
        if route.id == selected:
            return route, connection
    raise ValueError("已选择的远程转写路由不存在或已停用；请先在转写设置中更换或清除选择")


def validate_asr_selection(db: Session, core: Settings) -> None:
    """配置事务提交前验证远程转写选择，不允许留下不可用的引用。"""
    value = _stored(db, MODEL_CONFIGURATION_KEY)
    _validate_selection(_parse(value) if value else None, core.asr_remote_route_id)


def asr_worker_settings() -> dict[str, object]:
    """父进程解析选中连接，只向 ASR 子进程传递该连接所需的凭据。"""
    core = effective_settings()
    payload = {
        key: value
        for key, value in core.model_dump(mode="json").items()
        if key.startswith(("asr_", "whisper_")) or key in {"ffmpeg_path", "ffprobe_path"}
    }
    if core.asr_remote_route_id:
        selection = _validate_selection(configured_models(), core.asr_remote_route_id)
        assert selection is not None
        route, connection = selection
        payload.update(
            asr_remote_base_url=connection.base_url, asr_remote_api_key=connection.api_key, asr_remote_model=route.model
        )
    payload["asr_remote_route_id"] = ""
    return payload
