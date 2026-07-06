"""片头/片尾模板 API 路由(V0.1.8 P1.2)。

CRUD 管理片头/片尾文字模板,支持变量替换。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import select as _sql_select

from app.db.models import IntroTemplate
from app.db.session import get_session

router = APIRouter(prefix="/api/intro-templates", tags=["intro_templates"])

ALLOWED_KEYS = {
    "name",
    "is_default",
    "intro_enabled",
    "intro_text",
    "intro_duration_s",
    "intro_font_name",
    "intro_font_size",
    "intro_font_color",
    "intro_bg_color",
    "outro_enabled",
    "outro_text",
    "outro_duration_s",
    "outro_font_name",
    "outro_font_size",
    "outro_font_color",
    "outro_bg_color",
}


@router.get("")
def list_templates(request: Request) -> list[dict[str, object]]:
    """列出所有片头/片尾模板。"""
    with get_session() as db:
        templates = db.exec(_sql_select(IntroTemplate).order_by(IntroTemplate.id)).all()
        return [
            {
                "id": t.id,
                "name": t.name,
                "is_default": t.is_default,
                "intro_enabled": t.intro_enabled,
                "intro_text": t.intro_text,
                "intro_duration_s": t.intro_duration_s,
                "intro_font_name": t.intro_font_name,
                "intro_font_size": t.intro_font_size,
                "intro_font_color": t.intro_font_color,
                "intro_bg_color": t.intro_bg_color,
                "outro_enabled": t.outro_enabled,
                "outro_text": t.outro_text,
                "outro_duration_s": t.outro_duration_s,
                "outro_font_name": t.outro_font_name,
                "outro_font_size": t.outro_font_size,
                "outro_font_color": t.outro_font_color,
                "outro_bg_color": t.outro_bg_color,
            }
            for t in templates
        ]


@router.post("")
def create_template(request: Request) -> dict[str, object]:
    """创建默认片头/片尾模板。"""
    with get_session() as db:
        t = IntroTemplate(name="默认片头片尾")
        db.add(t)
        db.commit()
        db.refresh(t)
        return {"id": t.id, "name": t.name}


@router.get("/{template_id}")
def get_template(template_id: int, request: Request) -> dict[str, object]:
    """获取模板详情。"""
    with get_session() as db:
        t = db.get(IntroTemplate, template_id)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
        return {k: getattr(t, k) for k in ALLOWED_KEYS if hasattr(t, k)} | {"id": t.id}


@router.put("/{template_id}")
async def update_template(template_id: int, request: Request) -> dict[str, str]:
    """更新模板配置。"""
    body = await request.json()
    with get_session() as db:
        t = db.get(IntroTemplate, template_id)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
        if body.get("is_default"):
            for dt in db.exec(
                _sql_select(IntroTemplate).where(IntroTemplate.is_default == True)  # noqa: E712
            ).all():
                dt.is_default = False
                db.add(dt)
        for key, value in body.items():
            if key in ALLOWED_KEYS and hasattr(t, key):
                setattr(t, key, value)
        db.add(t)
        db.commit()
    return {"status": "updated"}


@router.delete("/{template_id}")
def delete_template(template_id: int, request: Request) -> dict[str, str]:
    """删除模板。"""
    with get_session() as db:
        t = db.get(IntroTemplate, template_id)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
        db.delete(t)
        db.commit()
    return {"status": "deleted"}
