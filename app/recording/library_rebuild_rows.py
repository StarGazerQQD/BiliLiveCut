# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.recording_storage import RecordingStorage
from app.db.schema import CURRENT_SCHEMA_VERSION
from app.recording.danmaku_journal import _MAX_LINE_BYTES, _Cursor, _Message, _record_adapter
from app.recording.session_storage import (
    CURSOR_PREFIX,
    LOCATION_PREFIX,
    OWNER_PREFIX,
    check_archive_header,
    check_journal_header,
    session_identity,
)
from app.sources.rooms import SourceBinding, _identity_key

# SQLite 中部分历史关联没有声明外键，复制前按来源逐项验证。
RELATIONS = {
    "live_rooms": {},
    "recording_sessions": {"room_id": "live_rooms"},
    "raw_segments": {"session_id": "recording_sessions"},
    "danmaku": {"session_id": "recording_sessions"},
    "transcripts": {"segment_id": "raw_segments"},
    "highlight_candidates": {"session_id": "recording_sessions"},
    "topics": {"session_id": "recording_sessions"},
    "highlight_events": {
        "session_id": "recording_sessions",
        "segment_id": "raw_segments",
        "topic_id": "topics",
        "candidate_id": "highlight_candidates",
    },
    "hotspot_events": {
        "session_id": "recording_sessions",
        "candidate_id": "highlight_candidates",
        "merged_into_id": "hotspot_events",
    },
    "highlight_topics": {"event_id": "highlight_events", "topic_id": "topics"},
    "final_clips": {"candidate_id": "highlight_candidates"},
    "clip_variants": {"event_id": "highlight_events"},
    "segment_tasks": {
        "session_id": "recording_sessions",
        "segment_id": "raw_segments",
        "event_id": "highlight_events",
        "candidate_id": "highlight_candidates",
        "clip_id": "final_clips",
    },
    "threshold_feedback": {"room_id": "live_rooms", "candidate_id": "highlight_candidates"},
    "upload_tasks": {"clip_id": "final_clips"},
    "upload_attempts": {"clip_id": "final_clips", "upload_task_id": "upload_tasks"},
}


def put_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    """写入新库专用的来源索引和已验证的历史元数据。"""
    db.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
        (key, value, datetime.now(UTC).replace(tzinfo=None).isoformat(" ")),
    )


def _setting(db: sqlite3.Connection, key: str) -> str | None:
    item = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return item[0] if item else None


def _verify_input(db: sqlite3.Connection, target: sqlite3.Connection) -> int:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables.issuperset({*RELATIONS, "schema_meta", "app_settings"}):
        raise ValueError("来源库缺少历史媒体库所需的数据表")
    row = db.execute("SELECT schema_version FROM schema_meta WHERE id=1").fetchone()
    if row is None or row[0] not in {CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION - 1}:
        raise ValueError("只支持当前及前一个 Schema 版本")
    for table, relations in RELATIONS.items():
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        expected = {r[1] for r in target.execute(f"PRAGMA table_info({table})")}
        if row[0] == 5:
            expected.update(
                {"recording_sessions": {"quality", "stream_url"}, "clip_variants": {"backup_path"}}.get(table, set())
            )
        if cols != expected:
            raise ValueError(f"Schema {row[0]} 的表 {table} 与预期结构不符")
        if db.execute(f"SELECT 1 FROM {table} WHERE id <= 0 LIMIT 1").fetchone():
            raise ValueError(f"{table} 含无效主键")
        for field, parent in relations.items():
            if db.execute(
                f"SELECT 1 FROM {table} c LEFT JOIN {parent} p ON c.{field}=p.id "
                f"WHERE c.{field} IS NOT NULL AND p.id IS NULL LIMIT 1"
            ).fetchone():
                raise ValueError(f"来源库关联缺失：{table}.{field} → {parent}")
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("来源数据库完整性检查失败")
    return row[0]


def _mapped_file(value: str, root: Path, mappings: dict[Path, Path]) -> Path:
    source = Path(value)
    if not value.strip() or ".." in source.parts:
        raise ValueError("历史媒体路径无效")
    path = root / source
    for old, new in sorted(mappings.items(), key=lambda pair: len(pair[0].parts), reverse=True):
        if path.is_relative_to(old):
            path = new / path.relative_to(old)
            break
    if path.resolve() != path or not any(path.is_relative_to(base) for base in (root, *mappings.values())):
        raise ValueError("媒体路径超出指定目录，请指定其实际原片或弹幕根目录")
    return path


def _settle(table: str, row: dict[str, Any], number: int) -> None:
    if table == "live_rooms":
        for key in (
            "enabled",
            "authorized",
            "schedule_enabled",
            "auto_record",
            "auto_analyze",
            "auto_approve",
            "auto_render",
            "auto_upload",
            "auto_threshold_enabled",
        ):
            row[key] = 0
    elif table == "recording_sessions":
        row["status"] = "archived"
    elif table == "raw_segments" and row["status"] == "recorded":
        row["status"] = "archived"
    elif table == "highlight_candidates":
        row["dedup_hash"] = f"library:{number}:{row['dedup_hash']}"
    elif table == "hotspot_events":
        row["event_key"] = f"library:{number}:{row['event_key']}"
    elif table == "segment_tasks":
        if row["stage"] not in {"completed", "failed", "cancelled"}:
            row["stage"] = "cancelled"
        row.update(
            pipeline_key=f"pipeline:{row['segment_id']}",
            stage_key=None,
            lease_token=None,
            claimed_at=None,
            claimed_by=None,
            heartbeat_at=None,
            next_retry_at=None,
        )
    elif table == "clip_variants" and row["render_status"] in {"queued", "rendering"}:
        row["render_status"] = "failed"
    elif table == "upload_tasks":
        if row["status"] in {"queued", "uploading", "preparing", "failed_retryable"}:
            row["status"] = "reconciliation_required"
        row.update(claimed_by=None, scheduled_at=None)
    elif table == "upload_attempts":
        if row["status"] in {"prepared", "in_progress"}:
            row["status"] = "reconciliation_required"
        row.update(lease_token=None, created_by_worker=None)


def _pending(
    db: sqlite3.Connection,
    target: sqlite3.Connection,
    old: int,
    new: int,
    room: int,
    physical: int,
    journal: Path,
    owner: str | None,
    check_cancelled: Callable[[], None] | None,
) -> None:
    value = _setting(db, f"{CURSOR_PREFIX}{old}")
    if not value and not journal.exists():
        return
    if journal.resolve() != journal or not journal.is_file():
        raise ValueError(f"场次 {old} 的补做记录缺失或重定向")
    platform, public_id = target.execute("SELECT platform,room_id FROM live_rooms WHERE id=?", (room,)).fetchone()
    old_room, started = db.execute("SELECT room_id,started_at FROM recording_sessions WHERE id=?", (old,)).fetchone()
    cursor = _Cursor.model_validate_json(value) if value else None
    with journal.open("rb") as stream:
        header = check_journal_header(stream, physical, owner)
        first, end = stream.tell(), stream.seek(0, 2)
        if cursor:
            if cursor.identity != header.identity or not first <= cursor.offset <= end:
                raise ValueError(f"场次 {old} 的补做游标无效")
            stream.seek(cursor.offset - 1)
            if stream.read(1) != b"\n":
                raise ValueError("弹幕补做游标未指向完整行边界")
            if cursor.unconfirmed_tail_bytes:
                if (
                    end - cursor.offset != cursor.unconfirmed_tail_bytes
                    or cursor.unconfirmed_tail_bytes > _MAX_LINE_BYTES
                ):
                    raise ValueError("弹幕补做尾行长度不符")
                if hashlib.sha256(stream.read()).hexdigest() != cursor.unconfirmed_tail_sha256:
                    raise ValueError("弹幕补做尾行身份不符")
                return
            if cursor.closed and cursor.offset != end:
                raise ValueError("已结束的补做记录有未登记数据")
        else:
            if db.execute("SELECT 1 FROM danmaku WHERE session_id=? LIMIT 1", (old,)).fetchone():
                raise ValueError(f"场次 {old} 同时有库内弹幕和无游标的补做记录，无法核实是否重复")
            stream.seek(first)
        index = 0
        while line := stream.readline(_MAX_LINE_BYTES + 1):
            if check_cancelled and index % 1024 == 0:
                check_cancelled()
            index += 1
            if len(line) > _MAX_LINE_BYTES or not line.endswith(b"\n"):
                raise ValueError(f"场次 {old} 的补做记录包含未确认的尾行")
            item = _record_adapter.validate_json(line)
            if isinstance(item, _Message):
                expected = public_id if platform == "bilibili" else old_room
                if item.room_id != expected or (
                    owner is None
                    and platform == "bilibili"
                    and item.ts.replace(tzinfo=item.ts.tzinfo or UTC)
                    < datetime.fromisoformat(started).replace(tzinfo=UTC)
                ):
                    raise ValueError(f"场次 {old} 的补做消息与历史房间或时间不符")
                stamp = (
                    item.ts.replace(tzinfo=item.ts.tzinfo or UTC).astimezone(UTC).replace(tzinfo=None).isoformat(" ")
                )
                target.execute(
                    "INSERT INTO danmaku(session_id,room_id,ts,msg_type,user,content,value) VALUES(?,?,?,?,?,?,?)",
                    (
                        new,
                        public_id if platform == "bilibili" else room,
                        stamp,
                        item.msg_type,
                        item.user,
                        item.content,
                        item.value,
                    ),
                )
            else:
                key = f"session_danmaku:{new}"
                target.execute("DELETE FROM app_settings WHERE key=?", (key,))
                put_setting(target, key, item.evidence.model_dump_json())


def import_library(
    db: sqlite3.Connection,
    target: sqlite3.Connection,
    number: int,
    root: Path,
    raw: Path | None,
    danmaku: Path | None,
    directory_maps: dict[Path, Path],
    check_cancelled: Callable[[], None] | None,
) -> dict[str, Any]:
    """复制历史业务行，分别映射来源库内部主键和原文件编号。"""
    version = _verify_input(db, target)
    offsets = {table: target.execute(f"SELECT coalesce(max(id),0) FROM {table}").fetchone()[0] for table in RELATIONS}
    rooms: dict[int, int] = {}
    platforms: dict[int, str] = {}
    session_rooms: dict[int, int] = {}
    paths: dict[int, dict[Path, Path]] = {}
    journals = []
    media_roots: dict[int, tuple[Path, Path]] = {}
    missing = 0
    for table, relations in RELATIONS.items():
        columns = [r[1] for r in target.execute(f"PRAGMA table_info({table})")]
        names = ",".join(columns)
        sql = f"INSERT INTO {table}({names}) VALUES({','.join('?' for _ in columns)})"
        for index, original in enumerate(db.execute(f"SELECT {names} FROM {table} ORDER BY id")):
            if check_cancelled and index % 1024 == 0:
                check_cancelled()
            row = dict(original)
            old_id = row["id"]
            row["id"] += offsets[table]
            for field, parent in relations.items():
                old_parent = row[field]
                if old_parent is not None:
                    row[field] = rooms[old_parent] if parent == "live_rooms" else old_parent + offsets[parent]
            if table == "live_rooms":
                platforms[old_id] = row["platform"]
                binding_json = _setting(db, f"source_room:{old_id}")
                if binding_json:
                    binding = SourceBinding.model_validate_json(binding_json)
                    key = _identity_key(binding.room)
                    if (
                        binding.room_db_id != old_id
                        or binding.room.platform != row["platform"]
                        or (row["platform"] == "bilibili" and binding.room.source_id != str(row["room_id"]))
                        or _setting(db, key) != binding_json
                    ):
                        raise ValueError(f"房间 {old_id} 的双向来源登记不一致")
                    previous = _setting(target, key)
                    if previous:
                        rooms[old_id] = SourceBinding.model_validate_json(previous).room_db_id
                        continue
                    updated = binding.model_copy(update={"room_db_id": row["id"]}).model_dump_json()
                    put_setting(target, key, updated)
                    put_setting(target, f"source_room:{row['id']}", updated)
                rooms[old_id] = row["id"]
            elif table == "recording_sessions":
                sid = row["id"]
                session_rooms[old_id] = original["room_id"]
                layout = RecordingStorage.model_validate_json(_setting(db, f"{LOCATION_PREFIX}{old_id}") or "{}")
                physical = layout.source_session_id or old_id
                original_raw, original_chat = root / layout.raw, root / layout.danmaku
                raw_root = raw or _mapped_file(str(original_raw), root, directory_maps)
                chat_root = danmaku or _mapped_file(str(original_chat), root, directory_maps)
                paths[old_id] = {**directory_maps, original_raw: raw_root}
                for old_base, copy_base in directory_maps.items():
                    if original_raw.is_relative_to(copy_base):
                        paths[old_id][old_base / original_raw.relative_to(copy_base)] = raw_root
                clips_original = Path(layout.source_clips) if layout.source_clips else root / "clips"
                clips = _mapped_file(str(clips_original), root, directory_maps)
                location = RecordingStorage(
                    raw=str(raw_root),
                    danmaku=str(chat_root),
                    import_id=layout.import_id,
                    source_session_id=physical,
                    source_clips=str(clips),
                    detached_import=layout.detached_import,
                )
                media_roots[old_id] = location.media_root(sid, root), raw_root
                owner = _setting(db, f"{OWNER_PREFIX}{old_id}")
                for folder in {location.session_dir(sid, root), location.session_dir(sid, root, danmaku=True)}:
                    if folder.exists() and session_identity(folder) != owner:
                        raise ValueError(f"场次 {old_id} 与所选原件目录身份不一致")
                archive = location.session_dir(sid, root, danmaku=True) / "danmaku.jsonl"
                if archive.is_file():
                    if archive.resolve() != archive:
                        raise ValueError("弹幕档案路径不能重定向")
                    with archive.open("rb") as stream:
                        header = check_archive_header(
                            stream, physical, owner, datetime.fromisoformat(row["started_at"])
                        )
                    room = db.execute(
                        "SELECT platform,room_id FROM live_rooms WHERE id=?", (original["room_id"],)
                    ).fetchone()
                    binding_json = _setting(db, f"source_room:{original['room_id']}")
                    source_id = SourceBinding.model_validate_json(binding_json).room.source_id if binding_json else None
                    if room["platform"] == "bilibili":
                        source_id = str(room["room_id"])
                    if header.platform != room["platform"] or (source_id is not None and header.source_id != source_id):
                        raise ValueError(f"场次 {old_id} 的弹幕原档与来源房间不符")
                put_setting(target, f"{LOCATION_PREFIX}{sid}", location.model_dump_json())
                if owner:
                    put_setting(target, f"{OWNER_PREFIX}{sid}", owner)
                if layout.source_session_id is None:
                    journals.append(
                        (old_id, sid, row["room_id"], physical, archive.with_name("danmaku-pending.jsonl"), owner)
                    )
                put_setting(
                    target,
                    f"library_rebuild:session:{sid}",
                    json.dumps({"source": number, "session_id": old_id, "status": row["status"]}),
                )
                for prefix in ("session_metadata:", "session_danmaku:", "local_source:", "session_timeline_summary:"):
                    value = _setting(db, f"{prefix}{old_id}")
                    if value:
                        put_setting(target, f"{prefix}{sid}", value)
                prefix = f"session_title_change:{old_id}:"
                for key, value in db.execute(
                    "SELECT key,value FROM app_settings WHERE substr(key,1,?)=?", (len(prefix), prefix)
                ):
                    put_setting(target, f"session_title_change:{sid}:{key[len(prefix) :]}", value)
            elif table == "danmaku":
                room = session_rooms[original["session_id"]]
                if platforms[room] != "bilibili":
                    row["room_id"] = rooms[room]
            elif table == "highlight_events" and row["features_json"]:
                features = json.loads(row["features_json"])
                workflow = features.pop("_review_workflow", None)
                if workflow is not None:
                    # 来源库的撤销快照含旧编号和运行状态，只保留历史动作，不接管领取或撤销权。
                    history = [
                        {k: v for k, v in item.items() if k not in {"task_states", "clip_statuses"}}
                        for item in workflow.get("history", [])
                    ]
                    put_setting(target, f"library_rebuild:review:{row['id']}", json.dumps(history, ensure_ascii=False))
                    row["features_json"] = json.dumps(features, ensure_ascii=False)
            if table in {"raw_segments", "clip_variants", "final_clips"}:
                mapping = paths[original["session_id"]] if table == "raw_segments" else directory_maps
                for field in ("file_path", "cover_path"):
                    if row.get(field):
                        path = _mapped_file(row[field], root, mapping)
                        if table == "raw_segments":
                            allowed, legacy_raw = media_roots[original["session_id"]]
                            if not path.is_relative_to(allowed) and path.parent != legacy_raw:
                                raise ValueError(f"分段 {old_id} 的文件不属于登记的场次目录")
                        row[field] = str(path)
                        missing += int(not path.is_file())
            _settle(table, row, number)
            target.execute(sql, tuple(row[column] for column in columns))
    for original_id, sid, room_id, physical, journal, owner in journals:
        _pending(db, target, original_id, sid, room_id, physical, journal, owner, check_cancelled)
    return {"schema": version, "id_offsets": offsets, "rooms": rooms, "missing_files": missing}
