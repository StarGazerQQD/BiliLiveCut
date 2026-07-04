"""阶段5:上传预检、查重、频控、manual 上传与设置开关测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def _make_clip(tmp_path: Path, title: str = "标题", desc: str = "简介", chash: str = "h1") -> int:
    """插入一个带真实文件的成品切片,返回其 id。"""
    from app.db.models import FinalClip
    from app.db.session import get_session

    f = tmp_path / f"{chash}.mp4"
    f.write_bytes(b"\x00" * 1024)
    with get_session() as db:
        clip = FinalClip(
            candidate_id=1,
            file_path=str(f),
            title=title,
            description=desc,
            content_hash=chash,
            tags_json='["a","b"]',
            duration_s=10.0,
        )
        db.add(clip)
        db.flush()
        return clip.id


def test_precheck_ok(temp_db: None, tmp_path: Path) -> None:
    """完整的切片通过全部预检。"""
    from app.publishing.uploader import precheck_clip

    cid = _make_clip(tmp_path)
    result = precheck_clip(cid)
    assert result.ok, result.reasons


def test_precheck_missing_title_and_file(temp_db: None, tmp_path: Path) -> None:
    """缺标题与缺文件都会被预检拦截。"""
    from app.db.models import FinalClip
    from app.db.session import get_session
    from app.publishing.uploader import precheck_clip

    with get_session() as db:
        clip = FinalClip(
            candidate_id=1,
            file_path=str(tmp_path / "nope.mp4"),
            title="",
            description="",
        )
        db.add(clip)
        db.flush()
        cid = clip.id
    result = precheck_clip(cid)
    assert not result.ok
    assert any("标题" in r for r in result.reasons)
    assert any("文件" in r for r in result.reasons)


def test_precheck_frequency_limit(temp_db: None, tmp_path: Path) -> None:
    """超过每小时投稿上限会被频控拦截。"""
    from app.core.config import settings
    from app.db.models import UploadStatus, UploadTask
    from app.db.session import get_session
    from app.publishing.uploader import precheck_clip

    cid = _make_clip(tmp_path)
    with get_session() as db:
        for _ in range(settings.upload_max_per_hour):
            db.add(
                UploadTask(
                    clip_id=cid,
                    status=UploadStatus.SUCCESS,
                    updated_at=datetime.now(UTC),
                )
            )
    result = precheck_clip(cid)
    assert not result.ok
    assert any("频率" in r for r in result.reasons)


def test_manual_upload_success_and_publish(temp_db: None, tmp_path: Path) -> None:
    """manual 上传成功:导出清单、任务 success、成品标记 published。"""
    from app.core.paths import ready_to_upload_dir
    from app.db.models import ClipStatus, FinalClip, UploadStatus
    from app.db.session import get_session
    from app.publishing.uploader import enqueue_and_upload

    cid = _make_clip(tmp_path)
    task = enqueue_and_upload(cid)
    assert task.status == UploadStatus.SUCCESS
    assert task.uploader == "manual"
    assert (ready_to_upload_dir() / f"clip_{cid}.json").exists()
    with get_session() as db:
        clip = db.get(FinalClip, cid)
        assert clip.status == ClipStatus.PUBLISHED


def test_settings_store_toggle(temp_db: None) -> None:
    """运行时开关默认关闭,设置后生效。"""
    from app.core import settings_store

    assert settings_store.biliup_enabled() is False
    assert settings_store.upload_active() is False
    settings_store.set_bool("biliup_enabled", True)
    assert settings_store.biliup_enabled() is True
    assert settings_store.upload_active() is True
