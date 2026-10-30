from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    if sys.argv[1] == "upload":
        with Path(sys.argv[2]).open("a", encoding="utf-8") as handle:
            handle.write("sent\n")
            handle.flush()
            os.fsync(handle.fileno())
        print("BV12345678901", flush=True)
        return

    from app.db.session import init_db
    from app.pipeline.workers.publish import execute_remote_upload

    init_db()
    result = execute_remote_upload(sys.argv[2])
    if result.get("outcome") != "success":
        raise RuntimeError(f"受控本地上传未成功: {result}")
    os._exit(29)


if __name__ == "__main__":
    main()
