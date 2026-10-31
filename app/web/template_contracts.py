# ruff: noqa: D100
from __future__ import annotations

import math
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveFloat = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


def _template_schema(schema: dict[str, object]) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for name, field in properties.items():
        if name == "description" or not isinstance(field, dict):
            continue
        alternatives = field.get("anyOf")
        if isinstance(alternatives, list):
            choices = [item for item in alternatives if item != {"type": "null"}]
            if len(choices) == 1:
                field.pop("anyOf")
                field.update(choices[0])
                field.pop("default", None)


class TemplateUpdate(BaseModel):
    """严格校验明确提供的字段，同时保留部分更新和可空描述。"""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, json_schema_extra=_template_schema)

    @model_validator(mode="before")
    @classmethod
    def reject_null_fields(cls, value: object) -> object:
        """区分未提供字段与显式写入空值。"""
        if isinstance(value, dict) and any(item is None and key != "description" for key, item in value.items()):
            raise ValueError("模板字段不能为 null，description 除外")
        return value


class SubtitleTemplateUpdate(TemplateUpdate):
    """字幕样式的部分更新；ASS 字形标志保持整数语义。"""

    name: StrictStr | None = None
    description: StrictStr | None = None
    font_name: StrictStr | None = None
    font_size: PositiveInt | None = None
    primary_color: StrictStr | None = None
    secondary_color: StrictStr | None = None
    outline_color: StrictStr | None = None
    back_color: StrictStr | None = None
    bold: StrictInt | None = None
    italic: StrictInt | None = None
    underline: StrictInt | None = None
    strikeout: StrictInt | None = None
    scale_x: NonNegativeInt | None = None
    scale_y: NonNegativeInt | None = None
    spacing: StrictInt | None = None
    angle: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = None
    border_style: StrictInt | None = None
    outline: NonNegativeFloat | None = None
    shadow: NonNegativeFloat | None = None
    alignment: Annotated[int, Field(strict=True, ge=1, le=9)] | None = None
    margin_l: NonNegativeInt | None = None
    margin_r: NonNegativeInt | None = None
    margin_v: NonNegativeInt | None = None
    encoding: NonNegativeInt | None = None
    max_chars_per_line: PositiveInt | None = None
    min_display_ms: PositiveInt | None = None
    max_display_ms: PositiveInt | None = None
    line_gap_ms: NonNegativeInt | None = None
    play_res_x: NonNegativeInt | None = None
    play_res_y: NonNegativeInt | None = None
    is_default: StrictBool | None = None

    @field_validator("bold", "italic", "underline", "strikeout")
    @classmethod
    def validate_ass_flag(cls, value: int) -> int:
        """保留 ASS 规定的整数开关。"""
        if value not in {0, -1}:
            raise ValueError("ASS 字形标志必须为 0 或 -1")
        return value

    @field_validator("border_style")
    @classmethod
    def validate_border_style(cls, value: int) -> int:
        """接受轮廓或不透明背景两种 ASS 边框。"""
        if value not in {1, 3}:
            raise ValueError("ASS 边框样式必须为 1 或 3")
        return value

    @field_validator("primary_color", "secondary_color", "outline_color", "back_color")
    @classmethod
    def validate_ass_color(cls, value: str) -> str:
        """验证 ASS 颜色的十六进制及整数表示。"""
        if re.fullmatch(r"&[Hh][0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?&?", value):
            return value
        if re.fullmatch(r"-?[0-9]+", value) and -(2**31) <= int(value) <= 2**32 - 1:
            return value
        raise ValueError("ASS 颜色必须为 ABGR 十六进制或有效的颜色整数")

    @model_validator(mode="after")
    def validate_display_interval(self) -> SubtitleTemplateUpdate:
        """同时更新显示边界时检查时长顺序。"""
        if self.min_display_ms is not None and self.max_display_ms is not None:
            if self.min_display_ms > self.max_display_ms:
                raise ValueError("最短显示时间不能大于最长显示时间")
        return self


class IntroTemplateUpdate(TemplateUpdate):
    """片头片尾参数的严格部分更新，允许原有文字变量与换行。"""

    name: StrictStr | None = None
    is_default: StrictBool | None = None
    intro_enabled: StrictBool | None = None
    intro_text: StrictStr | None = None
    intro_duration_s: PositiveFloat | None = None
    intro_font_name: StrictStr | None = None
    intro_font_size: PositiveInt | None = None
    intro_font_color: StrictStr | None = None
    intro_bg_color: StrictStr | None = None
    outro_enabled: StrictBool | None = None
    outro_text: StrictStr | None = None
    outro_duration_s: PositiveFloat | None = None
    outro_font_name: StrictStr | None = None
    outro_font_size: PositiveInt | None = None
    outro_font_color: StrictStr | None = None
    outro_bg_color: StrictStr | None = None

    @field_validator("intro_font_color", "intro_bg_color", "outro_font_color", "outro_bg_color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        """限制颜色语法及透明度，保留命名颜色。"""
        color, separator, alpha = value.partition("@")
        if not re.fullmatch(r"(?:0x|#)?[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?|[A-Za-z]+", color):
            raise ValueError("片头颜色格式无效")
        if separator:
            try:
                opacity = int(alpha, 16) / 255 if re.fullmatch(r"0x[0-9A-Fa-f]{1,2}", alpha) else float(alpha)
            except ValueError as exc:
                raise ValueError("片头颜色透明度无效") from exc
            if not math.isfinite(opacity) or not 0 <= opacity <= 1:
                raise ValueError("片头颜色透明度必须在 0 到 1 之间")
        return value
