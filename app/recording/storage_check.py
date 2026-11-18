# ruff: noqa: D100
from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory

from app.core.recording_storage import RecordingStorage
from app.recording.session_storage import (
    CURSOR_PREFIX,
    JOURNAL_COLLISION,
    LOCATION_PREFIX,
    OWNER_PREFIX,
    SESSION_TREES,
    check_archive_header,
    check_journal_header,
    session_identity,
)


@contextmanager
def _open_offline_database(database: Path) -> Iterator[sqlite3.Connection]:
    """读取停写副本，避免 SQLite 在原目录建立 WAL 索引或执行恢复写入。"""
    database = database.resolve(strict=True)
    journals = [database.with_name(database.name + suffix) for suffix in ("-wal", "-journal")]
    with ExitStack() as stack:
        pending = [path for path in journals if path.is_file() and path.stat().st_size]
        if pending:
            target = Path(stack.enter_context(TemporaryDirectory())) / database.name
            shutil.copyfile(database, target)
            for path in pending:
                shutil.copyfile(path, target.with_name(path.name))
            db = stack.enter_context(closing(sqlite3.connect(target)))
        else:
            uri = database.as_uri() + "?mode=ro&immutable=1"
            db = stack.enter_context(closing(sqlite3.connect(uri, uri=True)))
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        yield db


def _reference(value: str) -> PurePath:
    return PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)


def _copy_path(value: str, root: Path, mappings: dict[PurePath, Path]) -> Path | None:
    ref = _reference(value)
    if ".." in ref.parts:
        return None
    for original in sorted(mappings, key=lambda item: len(item.parts), reverse=True):
        if ref.is_relative_to(original):
            return mappings[original].joinpath(*ref.relative_to(original).parts)
    if ref.is_absolute() and not ref.is_relative_to(_reference(str(root))):
        return None
    if not ref.is_absolute() and ("\\" in value or PureWindowsPath(value).drive):
        return None
    return root / value


def check_storage(
    database: Path, root: Path, directory_maps: tuple[tuple[str, Path], ...] = ()
) -> tuple[int, list[str]]:
    """逐项比对数据库副本、场次目录和原始分段，输出编号及文件定位。"""
    root = root.resolve(strict=True)
    if not root.is_dir() or not database.is_file():
        raise ValueError("请选择已有的数据库文件和 storage 目录")
    mappings = {_reference(source): copy.resolve(strict=True) for source, copy in directory_maps}
    if len(mappings) != len(directory_maps) or any(
        not source.is_absolute() or not copy.is_dir() for source, copy in mappings.items()
    ):
        raise ValueError("外置目录映射需要唯一的原绝对路径和有效的副本目录")
    with _open_offline_database(database) as db:
        sessions = {
            sid: (error, datetime.fromisoformat(start))
            for sid, error, start in db.execute(
                "SELECT id, error_message, started_at FROM recording_sessions ORDER BY id"
            )
        }
        owners = {
            int(key.removeprefix(OWNER_PREFIX)): value
            for key, value in db.execute(
                "SELECT key, value FROM app_settings WHERE substr(key,1,?)=?", (len(OWNER_PREFIX), OWNER_PREFIX)
            )
        }
        locations = {
            int(key.removeprefix(LOCATION_PREFIX)): RecordingStorage.model_validate_json(value)
            for key, value in db.execute(
                "SELECT key,value FROM app_settings WHERE substr(key,1,?)=?", (len(LOCATION_PREFIX), LOCATION_PREFIX)
            )
        }
        cursors = {
            int(key.removeprefix(CURSOR_PREFIX)): (identity, offset)
            for key, identity, offset in db.execute(
                "SELECT key, json_extract(value,'$.identity'), json_extract(value,'$.offset') "
                "FROM app_settings WHERE substr(key,1,?)=?",
                (len(CURSOR_PREFIX), CURSOR_PREFIX),
            )
        }
        findings = [
            f"场次 {sid} 有弹幕补做记录冲突，须核对原场次"
            for sid, (error, _) in sessions.items()
            if JOURNAL_COLLISION in (error or "")
        ]
        for sid, count in db.execute(
            "SELECT d.session_id, count(*) FROM danmaku d JOIN recording_sessions s ON s.id=d.session_id "
            "WHERE julianday(d.ts)<julianday(s.started_at) GROUP BY d.session_id ORDER BY d.session_id"
        ):
            findings.append(f"场次 {sid} 有 {count} 条弹幕早于场次开始，请复核归属")
        registered: dict[Path, int] = {}
        for segment, sid, ref in db.execute("SELECT id, session_id, file_path FROM raw_segments ORDER BY id"):
            path = _copy_path(ref, root, mappings)
            if path is None:
                findings.append(f"场次 {sid} 分段 {segment} 引用副本目录外的文件，须核对外置目录映射")
                continue
            if path in registered and registered[path] != sid:
                findings.append(f"场次 {sid} 分段 {segment} 的文件也登记在场次 {registered[path]}")
            else:
                registered[path] = sid
            if sid not in sessions:
                findings.append(f"分段 {segment} 的场次 {sid} 缺少数据库记录")
            if path.resolve() != path:
                findings.append(f"场次 {sid} 分段 {segment} 的登记路径重定向")
            elif not path.is_file():
                findings.append(f"场次 {sid} 分段 {segment} 的登记文件在副本中缺失")

    raw_seen: set[int] = set()
    danmaku_seen: set[int] = set()
    trees = {root / name for name in SESSION_TREES}
    positions: dict[int, tuple[Path | None, Path | None]] = {}
    physical_ids: dict[tuple[Path, int], int] = {}
    for sid, location in locations.items():
        raw = _copy_path(location.raw, root, mappings)
        danmaku = _copy_path(location.danmaku, root, mappings)
        positions[sid] = raw, danmaku
        for base, kind in ((raw, "原片"), (danmaku, "弹幕")):
            if base is None:
                findings.append(f"场次 {sid} 的{kind}外置目录尚未映射到副本")
            else:
                trees.add(base)
                key = base, location.source_session_id or sid
                if key in physical_ids and physical_ids[key] != sid:
                    findings.append(f"同一物理场次目录关联多个数据库场次：{sid} 和 {physical_ids[key]}")
                physical_ids[key] = sid
    for tree in sorted(trees):
        if tree.resolve() != tree:
            raise ValueError("副本目录重定向，无法核对")
        if not tree.exists():
            continue
        for directory in sorted(tree.iterdir()):
            match = re.fullmatch(r"session_([1-9][0-9]*)", directory.name)
            if match is None:
                continue
            physical = int(match[1])
            sid = physical_ids.get((tree, physical), physical)
            label = directory.relative_to(root).as_posix() if directory.is_relative_to(root) else str(directory)
            if directory.resolve() != directory or not directory.is_dir():
                findings.append(f"{label} 路径无效或重定向")
                continue
            if sid not in sessions:
                findings.append(f"{label} 缺少对应的数据库场次")
            raw, danmaku = positions.get(sid, (root / "raw", root / "raw"))
            if tree not in {raw, danmaku}:
                if tree not in {root / "clips", root / "ready_to_upload"} and sid in sessions:
                    findings.append(f"{label} 与数据库登记的存储位置不一致")
                continue
            if tree == raw:
                raw_seen.add(sid)
            if tree == danmaku:
                danmaku_seen.add(sid)
            try:
                actual = session_identity(directory)
            except ValueError:
                findings.append(f"{label} 场次身份文件无效")
            else:
                if owners.get(sid) != actual:
                    findings.append(f"{label} 的场次身份与数据库不一致")
            for name in ("danmaku.jsonl", "danmaku-pending.jsonl") if tree == danmaku else ():
                path = directory / name
                if path.resolve() != path:
                    findings.append(f"{label}/{name} 路径重定向")
                    continue
                try:
                    with path.open("rb") as stream:
                        if name == "danmaku.jsonl":
                            check_archive_header(
                                stream, physical, owners.get(sid), sessions[sid][1] if sid in sessions else None
                            )
                        else:
                            header = check_journal_header(stream, physical, owners.get(sid))
                            if sid in cursors:
                                identity, offset = cursors[sid]
                                if (
                                    header.identity != identity
                                    or type(offset) is not int
                                    or not stream.tell() <= offset <= stream.seek(0, 2)
                                ):
                                    findings.append(f"{label}/{name} 与数据库补做游标不一致")
                            elif header.version == 1:
                                findings.append(f"{label}/{name} 为旧版补做记录，缺少数据库游标，须核对原场次")
                except FileNotFoundError:
                    if name == "danmaku-pending.jsonl" and sid in cursors:
                        findings.append(f"{label}/{name} 已登记补做游标，文件缺失")
                    continue
                except ValueError as exc:
                    findings.append(f"{label}/{name}：{exc}")
            for media in sorted(directory.iterdir()) if tree == raw else ():
                if media.suffix.lower() not in {".ts", ".flv", ".mp4", ".mkv", ".m4s", ".webm", ".mov"}:
                    continue
                if registered.get(media) != sid:
                    findings.append(f"{label}/{media.name} 缺少对应场次的分段登记")
    for sid in sorted(owners.keys() - raw_seen):
        findings.append(f"场次 {sid} 已登记存储身份，原始目录缺失")
    for sid in sorted((cursors.keys() | (locations.keys() & owners.keys())) - danmaku_seen):
        findings.append(f"场次 {sid} 已登记弹幕位置或补做游标，弹幕目录缺失或尚未映射")
    return len(sessions), findings
