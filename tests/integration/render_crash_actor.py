from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app.db.session import init_db
from app.pipeline.lease import TaskLease
from app.pipeline.workers.render import commit_render


def main() -> None:
    init_db()
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = payload["result"]
    replace = Path.replace

    def crash_after_move(path: Path, target: str | Path) -> Path:
        moved = replace(path, target)
        if str(path) == result["temp_path"]:
            os._exit(23)
        return moved

    # 仅注入文件系统边界的真实进程退出，跳过所有 Python finally。
    Path.replace = crash_after_move
    commit_render(TaskLease(**payload["lease"]), result, 1)
    raise RuntimeError("未到达预期中断点")


if __name__ == "__main__":
    main()
