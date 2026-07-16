"""CLI 命令模块 — 从 app/cli.py 拆分。

所有命令实现在各子模块中, 通过 COMMANDS 列表导出供 cli.py 统一注册。
"""

from app.commands.config import CONFIG_COMMANDS
from app.commands.database import DATABASE_COMMANDS
from app.commands.doctor import DOCTOR_COMMANDS
from app.commands.maintenance import MAINTENANCE_COMMANDS
from app.commands.ml import ML_COMMANDS
from app.commands.process import PROCESS_COMMANDS
from app.commands.record import RECORD_COMMANDS
from app.commands.room import ROOM_COMMANDS
from app.commands.serve import SERVE_COMMANDS

ALL_COMMANDS: list[tuple[str, callable, str | None]] = (
    RECORD_COMMANDS
    + PROCESS_COMMANDS
    + SERVE_COMMANDS
    + DOCTOR_COMMANDS
    + DATABASE_COMMANDS
    + CONFIG_COMMANDS
    + ROOM_COMMANDS
    + MAINTENANCE_COMMANDS
    + ML_COMMANDS
)

__all__ = ["ALL_COMMANDS"] + [
    "CONFIG_COMMANDS",
    "DATABASE_COMMANDS",
    "DOCTOR_COMMANDS",
    "MAINTENANCE_COMMANDS",
    "PROCESS_COMMANDS",
    "RECORD_COMMANDS",
    "ROOM_COMMANDS",
    "SERVE_COMMANDS",
]
