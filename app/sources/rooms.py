"""来源身份的事务登记；复用 AppSetting 主键约束，不改变 Schema 5。"""

from __future__ import annotations

import asyncio
import json
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.db.entities import AppSetting, LiveRoom
from app.db.session import get_session
from app.plugins.live_source import RoomSnapshot, SourceError, SourceInvalidInput, SourceRoom
from app.sources.registry import SourceRegistry, source_registry


class SourceBinding(BaseModel):
    """同一版本的正向/反向绑定，整组与 LiveRoom 在同一事务提交。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    room_db_id: int = Field(gt=0)
    room: SourceRoom


class RoomSourceView(TypedDict):
    """管理界面的稳定身份及插件可用性；缺失元数据不伪造平台房号。"""

    platform: str
    source_id: str | None
    canonical_url: str | None
    source_available: bool
    source_error: str | None


def room_source_view(room: LiveRoom, db: Session | None = None) -> RoomSourceView:
    """来源缺失时继续展示历史房间，明确报告无法启动的原因。"""
    try:
        source = room_source(room, db)
    except SourceError as exc:
        return {
            "platform": room.platform,
            "source_id": None,
            "canonical_url": None,
            "source_available": False,
            "source_error": str(exc),
        }
    available = source_registry.available(source.platform)
    return {
        "platform": source.platform,
        "source_id": source.source_id,
        "canonical_url": source.canonical_url,
        "source_available": available,
        "source_error": None if available else "来源不可用，请安装并启用对应直播源插件",
    }


def _identity_key(room: SourceRoom) -> str:
    # 无截断或哈希：JSON 数组保留分隔符、Unicode 和平台内 ID 的完整语义。
    identity = json.dumps([room.platform, room.source_id], ensure_ascii=False, separators=(",", ":"))
    return f"source_identity:{identity}"


def _binding(row: AppSetting) -> SourceBinding:
    try:
        return SourceBinding.model_validate_json(row.value)
    except ValidationError as exc:
        raise SourceInvalidInput("来源身份元数据损坏，请从备份恢复，禁止重新关联历史房间") from exc


def room_source(room: LiveRoom, db: Session | None = None) -> SourceRoom:
    """读取持久来源身份；旧 Bilibili 数字房号无需升级即可使用。"""
    if db is None:
        with get_session() as connection:
            return room_source(room, connection)
    row = db.get(AppSetting, f"source_room:{room.id}")
    if row is None:
        if room.platform == "bilibili" and room.room_id is not None:
            legacy = SourceRoom(
                platform="bilibili",
                source_id=str(room.room_id),
                canonical_url=f"https://live.bilibili.com/{room.room_id}",
            )
            index = db.get(AppSetting, _identity_key(legacy))
            if index is not None and _binding(index).room_db_id == room.id:
                raise SourceInvalidInput("来源身份缺少反向索引，请从备份恢复")
            return legacy
        raise SourceInvalidInput("直播间缺少来源身份，请启用对应插件并重新添加规范地址")
    binding = _binding(row)
    if binding.room_db_id != room.id or binding.room.platform != room.platform:
        raise SourceInvalidInput("来源身份与房间关联不一致")
    if room.platform == "bilibili" and binding.room.source_id != str(room.room_id):
        raise SourceInvalidInput("Bilibili 房号与来源身份不一致")
    index = db.get(AppSetting, _identity_key(binding.room))
    if index is None or _binding(index) != binding:
        raise SourceInvalidInput("来源身份索引不一致，请从备份恢复")
    return binding.room


def _register(source: SourceRoom, snapshot: RoomSnapshot, authorized: bool) -> LiveRoom:
    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            # SQLite 在读取去重结果之前拿写锁，避免并发登记旧 Bili 房间或升级读事务。
            # 此函数在工作线程执行，忙等待不阻塞宿主事件循环。
            db.execute(text("BEGIN IMMEDIATE"))
        key = _identity_key(source)
        index = db.get(AppSetting, key)
        existing: LiveRoom | None = None
        if index is not None:
            binding = _binding(index)
            if (binding.room.platform, binding.room.source_id) != (source.platform, source.source_id):
                raise SourceInvalidInput("来源身份索引损坏")
            existing = db.get(LiveRoom, binding.room_db_id)
            if existing is None or room_source(existing, db) != binding.room:
                raise SourceInvalidInput("来源身份索引引用无效房间")
        elif source.platform == "bilibili":
            existing = db.exec(
                select(LiveRoom)
                .where(LiveRoom.platform == "bilibili", LiveRoom.room_id == int(source.source_id))
                .order_by(LiveRoom.id)
            ).first()
        if index is None:
            # 新登记前检查同平台已有反向绑定，不能把丢失正向索引当作首次登记。
            for candidate in db.exec(select(LiveRoom).where(LiveRoom.platform == source.platform)).all():
                stored = db.get(AppSetting, f"source_room:{candidate.id}")
                if stored is not None:
                    binding = _binding(stored)
                    if binding.room.source_id == source.source_id or (existing and candidate.id == existing.id):
                        raise SourceInvalidInput("来源身份缺少正向索引，请从备份恢复")
        canonical_match = db.exec(
            select(LiveRoom).where(LiveRoom.platform == source.platform, LiveRoom.input_url == source.canonical_url)
        ).first()
        if canonical_match is not None and (existing is None or canonical_match.id != existing.id):
            raise SourceInvalidInput("规范地址已属于另一稳定房间身份，请检查插件解析结果")
        room = existing or LiveRoom(
            platform=source.platform,
            room_id=int(source.source_id) if source.platform == "bilibili" else None,
            input_url=source.canonical_url,
            highlight_threshold=settings.highlight_threshold,
            review_threshold=settings.highlight_review_threshold,
            auto_approve_threshold=settings.highlight_auto_approve_threshold,
            auto_publish_threshold=settings.auto_publish_threshold,
        )
        room.input_url = source.canonical_url
        room.authorized = authorized
        if snapshot.title is not None:
            room.title = snapshot.title
        if snapshot.uploader_name is not None:
            room.uploader_name = snapshot.uploader_name
        db.add(room)
        db.flush()
        if room.id is None:
            raise RuntimeError("数据库未分配房间主键")
        binding = SourceBinding(room_db_id=room.id, room=source)
        for binding_key in (key, f"source_room:{room.id}"):
            stored = db.get(AppSetting, binding_key) or AppSetting(key=binding_key)
            stored.value = binding.model_dump_json()
            db.add(stored)
        db.flush()
        return room


async def register_room(
    value: str, authorized: bool, platform: str | None = None, *, registry: SourceRegistry = source_registry
) -> LiveRoom:
    """CLI/Web 共用：解析规范身份后事务去重；只更新授权和资料，不开启任何自动化。"""
    if settings.require_authorization and not authorized:
        raise ValueError("需要确认授权才能添加直播间。")
    source = await registry.resolve_room(value, platform)
    snapshot = await registry.get_room_info(source)
    # 插件可能在网络查询间隙停用；持久化前再次确认来源仍接受调用。
    if not registry.available(source.platform):
        from app.plugins.live_source import SourceUnavailable

        raise SourceUnavailable("来源已停用，请重新启用后添加")
    try:
        return await asyncio.to_thread(_register, source, snapshot, authorized)
    except IntegrityError:
        # 非 SQLite 后端通过主键唯一约束串行化竞争，重读已提交的胜者，最多重试一次。
        return await asyncio.to_thread(_register, source, snapshot, authorized)
