"""前端静态 JavaScript 的语法回归测试。"""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = PROJECT_ROOT / "app" / "web" / "static"
JAVASCRIPT_FILES = tuple(sorted(STATIC_ROOT.rglob("*.js")))
TEMPLATE_FILES = tuple(sorted((PROJECT_ROOT / "app" / "web" / "templates").glob("*.html")))
INLINE_SCRIPT_TEMPLATES = tuple(path for path in TEMPLATE_FILES if "<script>" in path.read_text(encoding="utf-8"))
INTERACTION_CHECK = PROJECT_ROOT / "scripts" / "check_frontend_interactions.mjs"


@pytest.mark.parametrize("readonly_filesystem", [False, True], ids=["normal", "readonly-filesystem"])
def test_configuration_form_preserves_drafts_and_validates_atomic_saves(readonly_filesystem: bool) -> None:
    """运行真实设置模块，覆盖迟到响应、保存失败、字段焦点及凭据语义。"""
    node = shutil.which("node")
    assert node is not None, "设置交互测试需要 Node.js"
    script = PROJECT_ROOT / "scripts" / "check_configuration_interactions.mjs"
    command = [node, str(script)]
    driver = None
    if readonly_filesystem:
        command = [node, "--input-type=module"]
        driver = f"""
import fs from "node:fs/promises";
import {{ syncBuiltinESMExports }} from "node:module";
for (const method of ["cp", "copyFile", "mkdir", "mkdtemp", "writeFile", "rm"]) {{
  fs[method] = async () => {{ throw new Error(`unexpected filesystem mutation: ${{method}}`); }};
}}
syncBuiltinESMExports();
await import({json.dumps(script.as_uri())});
"""
    try:
        result = subprocess.run(
            command,
            input=driver,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        pytest.fail(f"设置交互检查超过 {exc.timeout} 秒。阶段输出：\n{stdout}\n{stderr}", pytrace=False)
    assert result.returncode == 0, result.stdout + result.stderr


class _ElementIdCollector(HTMLParser):
    """收集 HTML 元素 ID，用于防止选择器命中错误元素。"""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """记录当前开始标签的 ID。"""
        for name, value in attrs:
            if name == "id" and value is not None:
                self.ids.append(value)


@pytest.mark.parametrize(
    "javascript_path",
    JAVASCRIPT_FILES,
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_static_javascript_has_valid_module_syntax(javascript_path: Path) -> None:
    """每个静态 JavaScript 文件都必须能按 ES Module 语法解析。"""
    node = shutil.which("node")
    assert node is not None, "前端语法检查需要 Node.js"

    result = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=javascript_path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    relative_path = javascript_path.relative_to(PROJECT_ROOT).as_posix()
    assert result.returncode == 0, f"{relative_path} 不是有效的 ES Module:\n{result.stderr}"


@pytest.mark.parametrize(
    "template_path",
    TEMPLATE_FILES,
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_html_template_element_ids_are_unique(template_path: Path) -> None:
    """同一页面重复 ID 会使交互代码更新到错误元素。"""
    parser = _ElementIdCollector()
    parser.feed(template_path.read_text(encoding="utf-8"))
    duplicates = sorted({element_id for element_id in parser.ids if parser.ids.count(element_id) > 1})

    assert duplicates == [], f"{template_path.name} 存在重复 ID: {duplicates}"


@pytest.mark.parametrize(
    "template_path",
    INLINE_SCRIPT_TEMPLATES,
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_inline_template_javascript_has_valid_syntax(template_path: Path) -> None:
    """独立页面内联脚本也必须通过 JavaScript 语法解析。"""
    node = shutil.which("node")
    assert node is not None, "前端语法检查需要 Node.js"
    html = template_path.read_text(encoding="utf-8")
    source = html.split("<script>", 1)[1].split("</script>", 1)[0]
    source = source.replace("{{ candidate_id | int }}", "1").replace("{{ topic_id | int }}", "1")

    result = subprocess.run(
        [node, "--input-type=commonjs", "--check"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    relative_path = template_path.relative_to(PROJECT_ROOT).as_posix()
    assert result.returncode == 0, f"{relative_path} 的内联脚本语法无效:\n{result.stderr}"


def test_recording_pipeline_has_visible_switch_and_no_hardcoded_web_override() -> None:
    """Web 录制应采用可见全局开关，不再把 Pipeline 强制写死为开启。"""
    template = (PROJECT_ROOT / "app" / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    recording_js = (STATIC_ROOT / "js" / "recording.js").read_text(encoding="utf-8")

    assert "include 'configuration.html'" in template
    configuration = (STATIC_ROOT / "js" / "configuration.js").read_text(encoding="utf-8")
    assert '"recording_pipeline_enabled"' in configuration
    assert '"transcript_llm_refine_enabled"' in configuration
    assert "/api/settings/configuration" in configuration
    assert "RECORDING_PIPELINE_ENABLED" in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "TRANSCRIPT_LLM_REFINE_ENABLED" in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "pipeline: true" not in recording_js


def test_transcript_page_exposes_safe_retranscription_action() -> None:
    """实时转写页应暴露带确认提示的重转写操作。"""
    template = (PROJECT_ROOT / "app" / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    recording_js = (STATIC_ROOT / "js" / "recording.js").read_text(encoding="utf-8")
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "/retranscribe" in recording_js
    assert "window.confirm" in recording_js
    assert "window.retranscribeTranscript" in app_js
    assert 'api("PATCH", `/api/transcripts/${id}`' in recording_js
    assert "dirtyTranscriptIds.size > 0" in recording_js
    assert "hasOpenTranscriptEditor" in recording_js
    assert 'addEventListener("change", markTranscriptDirty)' in recording_js
    assert "transcriptEditorRevision !== revision" in recording_js
    assert "data-transcript-detail" in recording_js
    assert "openTranscriptDetails" in recording_js
    assert "transcriptListSignature" in recording_js
    assert 'id="transcript-session-select"' in template
    assert "/api/sessions/history" in recording_js
    assert "transcriptSelectedSessionId" in recording_js
    assert "请先保存或取消当前转写纠错" in recording_js
    assert "window.correctTranscript" in app_js


def test_transcript_page_shows_and_copies_source_ts_file_name() -> None:
    """每条实时转写都应明确对应源 TS 文件并提供复制入口。"""
    recording_js = (STATIC_ROOT / "js" / "recording.js").read_text(encoding="utf-8")
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "source_file_name" in recording_js
    assert "源 TS 文件" in recording_js
    assert "copyTranscriptSourceFile" in recording_js
    assert "/source-mp4" in recording_js
    assert "无损导出 MP4" in recording_js
    assert "navigator.clipboard" in recording_js
    assert "window.copyTranscriptSourceFile" in app_js


def test_danmaku_page_selects_and_retains_recording_session() -> None:
    """弹幕页应按录制场次筛选，并在轮询刷新时保留用户选择。"""
    template = (PROJECT_ROOT / "app" / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    recording_js = (STATIC_ROOT / "js" / "recording.js").read_text(encoding="utf-8")

    assert 'id="danmaku-session-select"' in template
    assert "danmakuSelectedSessionId" in recording_js
    assert "DANMAKU_SESSION_STORAGE_KEY" in recording_js
    assert "session_id=${encodeURIComponent(selectedSessionId)}" in recording_js
    assert "danmakuListSignature" in recording_js


def test_dashboard_uses_session_timeline_as_primary_review_view() -> None:
    """制作主视图应按录制场次展示时间线，同时保留逐条精审入口。"""
    template = (PROJECT_ROOT / "app" / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    timeline_js = (STATIC_ROOT / "js" / "timeline.js").read_text(encoding="utf-8")

    assert 'data-tab="candidates">场次时间线' in template
    assert 'id="timeline-list"' in template
    assert 'id="timeline-include-rejected"' in template
    assert 'id="candidates-list"' not in template
    assert "loadSessionTimelines" in app_js
    assert "/api/sessions/timeline" in timeline_js
    assert "representative_danmaku" in timeline_js
    assert "requestSessionReanalysis" in timeline_js
    assert "expandedProvenanceCandidates" in timeline_js
    assert "data-provenance-candidate" in timeline_js
    assert "hotspot_event_id" in timeline_js
    assert "仅时间线，不生成视频" in timeline_js
    assert "event_status" in timeline_js
    assert "sessionListSignature" in timeline_js
    assert "timelineDetailSignatures" in timeline_js
    assert "preservedDetails" in timeline_js
    assert "captureTimelineViewport" in timeline_js
    assert "restoreTimelineViewport" in timeline_js
    assert "whole_session_summary" in timeline_js
    assert "全场高光总结" in timeline_js
    assert "regenerateSessionSummary" in timeline_js
    assert "/timeline-summary" in timeline_js
    assert "window.regenerateSessionSummary" in app_js


def test_room_dictionary_ui_exposes_manual_and_learned_aliases() -> None:
    """房间配置应说明 ASR 热词作用，并展示人工纠错自动学习结果。"""
    rooms_js = (STATIC_ROOT / "js" / "rooms.js").read_text(encoding="utf-8")

    assert "ASR 热词" in rooms_js
    assert "learned_aliases" in rooms_js
    assert "人工纠错词典" in rooms_js


def test_room_and_feature_forms_pause_refresh_while_dirty() -> None:
    """直播间录制选项或独立开关存在草稿时，不得被五秒轮询覆盖。"""
    rooms_js = (STATIC_ROOT / "js" / "rooms.js").read_text(encoding="utf-8")

    assert "dirtyRoomSections.size > 0" in rooms_js
    assert "data-room-dirty-section" in rooms_js
    assert "dirtyFeatureRooms.size > 0" in rooms_js
    assert "data-feature-room-id" in rooms_js
    assert "openRoomDetails" in rooms_js
    assert "data-room-detail" in rooms_js
    assert "selectedRoom" in rooms_js
    assert "selectedSession" in rooms_js
    assert "roomEditorRevision !== revision" in rooms_js
    assert "featureEditorRevision !== revision" in rooms_js


def test_all_editable_pages_guard_local_drafts_against_late_responses() -> None:
    """轮询页和独立编辑页都应保留请求期间产生的新草稿。"""
    dashboard_js = (STATIC_ROOT / "js" / "dashboard.js").read_text(encoding="utf-8")
    configuration_js = (STATIC_ROOT / "js" / "configuration.js").read_text(encoding="utf-8")
    plugin_settings_js = (STATIC_ROOT / "js" / "plugin_settings.js").read_text(encoding="utf-8")
    review_html = (PROJECT_ROOT / "app" / "web" / "templates" / "review.html").read_text(encoding="utf-8")
    collection_html = (PROJECT_ROOT / "app" / "web" / "templates" / "collection.html").read_text(encoding="utf-8")

    assert "collectionTopicRevision === revision" in dashboard_js
    assert '"trend_schedule_enabled"' in configuration_js
    assert '"auto_upload"' in configuration_js
    assert "hasConfigurationDraft" in (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "settingsRevision !== revision" in plugin_settings_js
    assert 'addEventListener("beforeunload"' in plugin_settings_js
    assert "reviewReasonRevision !== reasonRevision" in review_html
    assert "preserveBoundary" in review_html
    assert "hasCollectionDraft" in collection_html
    assert 'addEventListener("beforeunload"' in collection_html

    settings_js = (STATIC_ROOT / "js" / "settings.js").read_text(encoding="utf-8")
    rooms_js = (STATIC_ROOT / "js" / "rooms.js").read_text(encoding="utf-8")
    review_queue_html = (PROJECT_ROOT / "app" / "web" / "templates" / "review_queue.html").read_text(encoding="utf-8")
    assert "_llmDirty || _llmRevision !== revision" in settings_js
    assert "newRoomFormRevision === revision" in rooms_js
    assert "scheduleLoadGeneration" in rooms_js
    assert "topicLoadGeneration" in rooms_js
    assert "queueLoadGeneration" in review_queue_html

    plugins_js = (STATIC_ROOT / "js" / "plugins.js").read_text(encoding="utf-8")
    assert "pendingPluginIds" in plugins_js
    assert "pluginMutationRevision !== revision" in plugins_js
    assert "reviewLoadGeneration" in review_html
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "hasUnsavedDashboardDraft" in app_js
    assert 'window.addEventListener("beforeunload"' in app_js
    assert "hasTranscriptDraft" in app_js


def test_frontend_module_graph_and_tab_interaction() -> None:
    """真实加载全部 ES Module，并验证初始刷新、事件绑定和标签切换。"""
    node = shutil.which("node")
    assert node is not None, "前端交互检查需要 Node.js"

    result = subprocess.run(
        [node, str(INTERACTION_CHECK)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: frontend module graph" in result.stdout
