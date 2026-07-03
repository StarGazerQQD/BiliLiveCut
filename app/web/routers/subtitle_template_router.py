"""ASS 字幕模板 API 路由(V0.1.8 P0)。

提供模板 CRUD、ASS 文件导入样式提取、导出 ASS 样式。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from loguru import logger
from sqlmodel import select as _sql_select

from app.db.models import SubtitleTemplate
from app.db.session import get_session

router = APIRouter(prefix="/api/templates", tags=["subtitle_templates"])

# --------------------------------------------------------------------------- #
# 样式提取正则(V4+ Styles)
# --------------------------------------------------------------------------- #
_STYLE_LINE_RE = re.compile(
    r"^Style:\s*(?P<Name>[^,]+),\s*"
    r"(?P<Fontname>[^,]*),\s*(?P<Fontsize>[^,]*),\s*(?P<PrimaryColour>[^,]*),\s*"
    r"(?P<SecondaryColour>[^,]*),\s*(?P<OutlineColour>[^,]*),\s*(?P<BackColour>[^,]*),\s*"
    r"(?P<Bold>[^,]*),\s*(?P<Italic>[^,]*),\s*(?P<Underline>[^,]*),\s*"
    r"(?P<StrikeOut>[^,]*),\s*(?P<ScaleX>[^,]*),\s*(?P<ScaleY>[^,]*),\s*"
    r"(?P<Spacing>[^,]*),\s*(?P<Angle>[^,]*),\s*(?P<BorderStyle>[^,]*),\s*"
    r"(?P<Outline>[^,]*),\s*(?P<Shadow>[^,]*),\s*(?P<Alignment>[^,]*),\s*"
    r"(?P<MarginL>[^,]*),\s*(?P<MarginR>[^,]*),\s*(?P<MarginV>[^,]*),\s*"
    r"(?P<Encoding>\d+)"
)

# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _parse_ass_styles(text: str) -> list[dict[str, str]]:
    """从 ASS 文本中提取所有 Style 行。

    :param text: ASS 文件内容。
    :returns: 解析后的样式列表(每个元素为字段名→值的 dict)。
    """
    styles: list[dict[str, str]] = []
    in_styles = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[V4+ Styles]") or stripped.startswith("[V4 Styles]") or stripped.startswith("[V4+ Styles]"):
            in_styles = True
            continue
        if in_styles and stripped.startswith("["):
            in_styles = False
            continue
        if in_styles and stripped.startswith("Style:"):
            match = _STYLE_LINE_RE.match(stripped)
            if match:
                styles.append(match.groupdict())
    return styles


def _model_to_ass_style_line(t: SubtitleTemplate) -> str:
    """将模板模型序列化为 ASS Style 行文本。

    :param t: SubtitleTemplate 实例。
    :returns: ``Style: Name,...`` 格式的字符串。
    """
    return (
        f"Style: {t.name},"
        f"{t.font_name},{t.font_size},{t.primary_color},{t.secondary_color},"
        f"{t.outline_color},{t.back_color},{t.bold},{t.italic},{t.underline},"
        f"{t.strikeout},{t.scale_x},{t.scale_y},{t.spacing},{t.angle:.1f},"
        f"{t.border_style},{t.outline:.1f},{t.shadow:.1f},{t.alignment},"
        f"{t.margin_l},{t.margin_r},{t.margin_v},{t.encoding}"
    )


def _model_to_ass_full(t: SubtitleTemplate) -> str:
    """将模板导出为完整 ASS 文件文本。

    :param t: SubtitleTemplate 实例。
    :returns: 完整的 ASS 文件内容。
    """
    return (
        "[Script Info]\n"
        f"Title: {t.name}\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {t.play_res_x}\n"
        f"PlayResY: {t.play_res_y}\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{_model_to_ass_style_line(t)}\n"
    )


# --------------------------------------------------------------------------- #
# API 端点
# --------------------------------------------------------------------------- #


@router.get("")
def list_templates(request: Request) -> list[dict[str, object]]:
    """列出所有字幕模板。

    :param request: FastAPI 请求对象。
    :returns: 模板列表。
    """
    with get_session() as db:
        templates = db.exec(_sql_select(SubtitleTemplate).order_by(SubtitleTemplate.id)).all()
        return [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "font_name": t.font_name,
                "font_size": t.font_size,
                "primary_color": t.primary_color,
                "outline_color": t.outline_color,
                "outline": t.outline,
                "shadow": t.shadow,
                "is_default": t.is_default,
                "max_chars_per_line": t.max_chars_per_line,
                "min_display_ms": t.min_display_ms,
                "max_display_ms": t.max_display_ms,
            }
            for t in templates
        ]


@router.post("")
def create_template(request: Request) -> dict[str, object]:
    """创建默认字幕模板。

    :param request: FastAPI 请求对象。
    :returns: 创建的模板。
    """
    with get_session() as db:
        t = SubtitleTemplate(name="默认横屏字幕", description="默认 ASS 字幕模板,适用于 1920×1080 横屏视频。")
        db.add(t)
        db.commit()
        db.refresh(t)
        return {"id": t.id, "name": t.name}


@router.get("/{template_id}")
def get_template(template_id: int, request: Request) -> dict[str, object]:
    """获取模板详情。

    :param template_id: 模板 ID。
    :param request: FastAPI 请求对象。
    :returns: 模板完整配置。
    """
    with get_session() as db:
        t = db.get(SubtitleTemplate, template_id)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
        return {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "font_name": t.font_name,
            "font_size": t.font_size,
            "primary_color": t.primary_color,
            "secondary_color": t.secondary_color,
            "outline_color": t.outline_color,
            "back_color": t.back_color,
            "bold": t.bold,
            "italic": t.italic,
            "underline": t.underline,
            "strikeout": t.strikeout,
            "scale_x": t.scale_x,
            "scale_y": t.scale_y,
            "spacing": t.spacing,
            "angle": t.angle,
            "border_style": t.border_style,
            "outline": t.outline,
            "shadow": t.shadow,
            "alignment": t.alignment,
            "margin_l": t.margin_l,
            "margin_r": t.margin_r,
            "margin_v": t.margin_v,
            "encoding": t.encoding,
            "max_chars_per_line": t.max_chars_per_line,
            "min_display_ms": t.min_display_ms,
            "max_display_ms": t.max_display_ms,
            "line_gap_ms": t.line_gap_ms,
            "play_res_x": t.play_res_x,
            "play_res_y": t.play_res_y,
            "is_default": t.is_default,
        }


@router.put("/{template_id}")
async def update_template(template_id: int, request: Request) -> dict[str, str]:
    """更新模板配置。

    :param template_id: 模板 ID。
    :param request: FastAPI 请求对象(JSON body)。
    :returns: 状态确认。
    """
    body = await request.json()
    allowed = {
        "name", "description", "font_name", "font_size", "primary_color",
        "secondary_color", "outline_color", "back_color", "bold", "italic",
        "underline", "strikeout", "scale_x", "scale_y", "spacing", "angle",
        "border_style", "outline", "shadow", "alignment", "margin_l",
        "margin_r", "margin_v", "encoding", "max_chars_per_line",
        "min_display_ms", "max_display_ms", "line_gap_ms", "play_res_x",
        "play_res_y", "is_default",
    }
    with get_session() as db:
        t = db.get(SubtitleTemplate, template_id)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")

        if body.get("is_default"):
            # 只有一个默认模板
            defaults = db.exec(
                _sql_select(SubtitleTemplate).where(SubtitleTemplate.is_default == True)  # noqa: E712
            ).all()
            for dt in defaults:
                dt.is_default = False
                db.add(dt)

        for key, value in body.items():
            if key in allowed and hasattr(t, key):
                setattr(t, key, value)
        db.add(t)
        db.commit()

    return {"status": "updated"}


@router.delete("/{template_id}")
def delete_template(template_id: int, request: Request) -> dict[str, str]:
    """删除模板。

    :param template_id: 模板 ID。
    :param request: FastAPI 请求对象。
    :returns: 状态确认。
    """
    with get_session() as db:
        t = db.get(SubtitleTemplate, template_id)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
        db.delete(t)
        db.commit()
    return {"status": "deleted"}


@router.post("/import/ass")
async def import_ass_file(file: UploadFile = File(...)) -> dict[str, object]:
    """导入 ASS 文件,提取其样式配置并创建模板。

    会自动解析 [V4+ Styles] 段落的 Style 行,提取所有字段。
    如果有多个 Style 行,为每个 Style 创建一个模板。

    :param file: 上传的 .ass 文件。
    :returns: 创建的模板列表。
    """
    if not file.filename or not file.filename.lower().endswith(".ass"):
        raise HTTPException(status_code=400, detail="请上传 .ass 文件")

    content = (await file.read()).decode("utf-8", errors="replace")
    styles = _parse_ass_styles(content)

    if not styles:
        raise HTTPException(status_code=400, detail="未在 ASS 文件中找到 [V4+ Styles] 段落的 Style 行")

    created: list[dict[str, object]] = []
    with get_session() as db:
        for style in styles:
            t = SubtitleTemplate(
                name=style.get("Name", "导入样式"),
                description=f"从 {file.filename} 导入",
                font_name=style.get("Fontname", "Noto Sans SC"),
                font_size=int(style.get("Fontsize", 36)),
                primary_color=style.get("PrimaryColour", "&H00FFFFFF"),
                secondary_color=style.get("SecondaryColour", "&H000000FF"),
                outline_color=style.get("OutlineColour", "&H00000000"),
                back_color=style.get("BackColour", "&H80000000"),
                bold=int(style.get("Bold", 0)),
                italic=int(style.get("Italic", 0)),
                underline=int(style.get("Underline", 0)),
                strikeout=int(style.get("StrikeOut", 0)),
                scale_x=int(style.get("ScaleX", 100)),
                scale_y=int(style.get("ScaleY", 100)),
                spacing=int(style.get("Spacing", 0)),
                angle=float(style.get("Angle", 0.0)),
                border_style=int(style.get("BorderStyle", 1)),
                outline=float(style.get("Outline", 2.0)),
                shadow=float(style.get("Shadow", 2.0)),
                alignment=int(style.get("Alignment", 2)),
                margin_l=int(style.get("MarginL", 20)),
                margin_r=int(style.get("MarginR", 20)),
                margin_v=int(style.get("MarginV", 20)),
                encoding=int(style.get("Encoding", 1)),
            )
            db.add(t)
            db.commit()
            db.refresh(t)
            created.append({"id": t.id, "name": t.name})
            logger.info("从 {} 导入 ASS 样式: {} (id={})", file.filename, t.name, t.id)

    return {"imported": created}


@router.get("/{template_id}/export")
def export_template(template_id: int, request: Request) -> PlainTextResponse:
    """导出模板为 ASS 文件格式。

    :param template_id: 模板 ID。
    :param request: FastAPI 请求对象。
    :returns: 完整 ASS 文本。
    """
    with get_session() as db:
        t = db.get(SubtitleTemplate, template_id)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
        text = _model_to_ass_full(t)
    return PlainTextResponse(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={t.name}.ass"},
    )
