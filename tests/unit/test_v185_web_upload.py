"""字幕与上传入口的审计回归。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select


def test_subtitle_default_switch_and_unicode_export(temp_db: None) -> None:
    from app.db.entities import SubtitleTemplate
    from app.db.session import get_session
    from app.web.main import app

    with get_session() as db:
        first = SubtitleTemplate(name="first", is_default=True)
        second = SubtitleTemplate(name='中文 标题"\r\n字幕')
        db.add(first)
        db.add(second)
        db.flush()
        first_id, second_id = first.id, second.id
    with TestClient(app) as client:
        response = client.put(f"/api/templates/{second_id}", json={"is_default": True})
        assert response.status_code == 200
        with get_session() as db:
            assert [row.id for row in db.exec(select(SubtitleTemplate).where(SubtitleTemplate.is_default)).all()] == [
                second_id
            ]
        assert client.put(f"/api/templates/{first_id}", json={"is_default": False}).status_code == 200
        exported = client.get(f"/api/templates/{second_id}/export")
        assert exported.status_code == 200
        header = exported.headers["content-disposition"]
        assert "filename*=UTF-8''" in header and "中文" in unquote(header)
        assert "\r" not in unquote(header) and "\n" not in unquote(header)


@pytest.mark.parametrize(
    "template",
    [
        'biliup upload "{file}" --title "{title}" --desc "{desc}"',
        r'"C:\Program Files\biliup.exe" upload {file} --title={title} --desc={desc}',
    ],
)
@pytest.mark.parametrize("relative_reference", [False, True])
def test_biliup_preserves_exact_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: str, relative_reference: bool
) -> None:
    from app.publishing import uploader

    media = tmp_path / "视频 文件" / "录播.mp4"
    media.parent.mkdir()
    media.write_bytes(b"media")
    reference = media.relative_to(tmp_path).as_posix() if relative_reference else str(media)
    clip = {
        "id": 1,
        "file_path": reference,
        "title": '标题 "引用" & 特殊',
        "description": "说明\n下一行",
    }
    received: list[list[str]] = []

    def execute(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        received.append(args)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(args, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", template)
    monkeypatch.setattr(uploader.subprocess, "run", execute)
    assert uploader.BiliupUploader().upload(clip).success
    assert len(received) == 1
    assert received[0][0] == (r"C:\Program Files\biliup.exe" if template.startswith('"') else "biliup")
    assert received[0][1] == "upload"
    assert received[0][2] == str(media.resolve())
    assert clip["title"] in received[0] or "--title=" + clip["title"] in received[0]
    assert clip["description"] in received[0] or "--desc=" + clip["description"] in received[0]
    if template.startswith('"'):
        assert received == [
            [
                r"C:\Program Files\biliup.exe",
                "upload",
                str(media.resolve()),
                '--title=标题 "引用" & 特殊',
                "--desc=说明\n下一行",
            ]
        ]
    else:
        assert received == [
            ["biliup", "upload", str(media.resolve()), "--title", '标题 "引用" & 特殊', "--desc", "说明\n下一行"]
        ]


@pytest.mark.parametrize("unknown_result", [False, True])
def test_restored_file_can_retry_skipped_upload_without_resending_unknown(
    temp_db: None,
    tmp_path: Path,
    unknown_result: bool,
) -> None:
    from app.db.entities import FinalClip, UploadAttempt
    from app.db.session import get_session
    from app.publishing.uploader import enqueue_upload, process_upload_task

    media = tmp_path / "restored.mp4"
    with get_session() as db:
        clip = FinalClip(candidate_id=1, file_path=str(media), title="test", description="test", duration_s=20)
        db.add(clip)
        db.flush()
        clip_id = clip.id
    task = enqueue_upload(clip_id)
    assert task.status == "skipped"
    if unknown_result:
        with get_session() as db:
            db.add(
                UploadAttempt(
                    upload_task_id=task.id,
                    clip_id=clip_id,
                    publish_generation=1,
                    attempt_token="unknown",
                    status="reconciliation_required",
                )
            )
    media.write_bytes(b"media restored")
    renewed = enqueue_upload(clip_id)
    assert renewed.id == task.id
    assert renewed.status == ("skipped" if unknown_result else "queued")
    processed = process_upload_task(task.id)
    assert processed.status == ("skipped" if unknown_result else "success")


def test_manual_export_exception_rolls_back_claim_and_allows_retry(temp_db: None, tmp_path: Path) -> None:
    from app.db.entities import FinalClip, UploadTask
    from app.db.session import get_session
    from app.publishing.uploader import enqueue_upload, process_upload_task

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"media")
    with get_session() as db:
        clip = FinalClip(candidate_id=1, file_path=str(media), title="t", description="d", tags_json="bad-json")
        db.add(clip)
        db.flush()
        clip_id = clip.id
    task = enqueue_upload(clip_id)
    with pytest.raises(ValueError):
        process_upload_task(task.id)
    with get_session() as db:
        saved = db.get(UploadTask, task.id)
        assert saved.status == "queued" and saved.claimed_by is None
        clip = db.get(FinalClip, clip_id)
        clip.tags_json = "[]"
        db.add(clip)
    assert process_upload_task(task.id).status == "success"


@pytest.mark.parametrize("template", ['biliup "{file}', "biliup {file.missing}", "biliup {unknown}"])
def test_invalid_upload_template_is_definitely_unsent(monkeypatch: pytest.MonkeyPatch, template: str) -> None:
    from app.publishing import uploader

    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", template)
    monkeypatch.setattr(uploader.subprocess, "run", lambda *args, **kwargs: pytest.fail("invalid command executed"))
    result = uploader.BiliupUploader().upload({"id": 1, "file_path": "sample.mp4"})
    assert result.outcome == "failed_permanent" and not result.request_may_have_been_sent
