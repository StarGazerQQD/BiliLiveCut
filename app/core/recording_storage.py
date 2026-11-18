# ruff: noqa: D100
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field


def validate_location(value: str) -> str:
    """允许本机绝对路径和存储根下的相对路径，拒绝含糊的盘符、控制字符及父目录。"""
    value = value.strip()
    local = Path(value)
    windows = PureWindowsPath(value)
    if (
        any(ord(char) < 32 for char in value)
        or ".." in local.parts
        or ".." in windows.parts
        or (windows.drive and not local.is_absolute())
        or (windows.root and not local.is_absolute())
        or ("\\" in value and os.name != "nt")
    ):
        raise ValueError("请输入服务主机的绝对目录，或存储根内不含 .. 的相对目录")
    return value


class RecordingStorage(BaseModel):
    """创建场次或导入时固定的目录；相对引用随完整 storage 副本迁移。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    raw: str = "raw"
    danmaku: str = "raw"
    import_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @classmethod
    def configured(cls, *, import_id: str | None = None) -> RecordingStorage:
        """将本次使用的有效配置转换为可随场次保存的目录引用。"""
        from app.core.config import settings

        root = Path(settings.storage_root).expanduser().resolve()

        def reference(value: str) -> str:
            base = cls.base(value or "raw", root)
            return base.relative_to(root).as_posix() if base.is_relative_to(root) else str(base)

        return cls(
            raw=reference(settings.storage_raw_dir),
            danmaku=reference(settings.storage_danmaku_dir),
            import_id=import_id,
        )

    @staticmethod
    def base(reference: str, storage: Path) -> Path:
        """保留外置卷路径，拒绝目录重定向。"""
        path = storage.expanduser().resolve() / validate_location(reference)
        if path.resolve() != path:
            raise ValueError("录制存储目录不能通过链接重定向")
        return path

    def session_dir(self, session_id: int, storage: Path, *, danmaku: bool = False) -> Path:
        """只构造路径，查询历史场次不会创建丢失的目录。"""
        if type(session_id) is not int or session_id <= 0:
            raise ValueError("场次 ID 必须为正整数")
        directory = self.base(self.danmaku if danmaku else self.raw, storage) / f"session_{session_id}"
        if directory.resolve() != directory:
            raise ValueError("场次目录不能通过链接重定向")
        return directory

    def media_root(self, session_id: int, storage: Path) -> Path:
        """限定已登记原片的导出和清理范围。"""
        if self.import_id is None:
            return self.session_dir(session_id, storage)
        return self.import_dir(self.import_id, storage) / "segments"

    def import_dir(self, import_id: str, storage: Path, *, danmaku: bool = False) -> Path:
        """定位由服务器分配的导入子目录，拒绝目录重定向。"""
        from uuid import UUID

        if UUID(import_id).hex != import_id:
            raise ValueError("导入编号无效")
        path = self.base(self.danmaku if danmaku else self.raw, storage) / "imports" / import_id
        if path.resolve() != path:
            raise ValueError("导入目录超出媒体根目录")
        return path
