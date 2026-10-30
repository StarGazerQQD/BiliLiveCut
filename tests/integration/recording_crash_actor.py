from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from app.core.config import settings
from app.db.entities import LiveRoom
from app.db.session import get_session, init_db
from app.recording.recorder import Recorder
from app.sources.registry import source_registry
from app.sources.rooms import room_source
from tests.integration.test_source_runtime import RuntimeSource


async def main() -> None:
    init_db()
    source_registry.register_many("crash-fixture", [RuntimeSource()])
    with get_session() as db:
        room = db.get(LiveRoom, int(sys.argv[1]))
        assert room is not None
        identity = room_source(room, db)
    original = asyncio.create_subprocess_exec
    observers: list[asyncio.Task[None]] = []

    async def crash_after_media(listing: Path) -> None:
        for _ in range(2000):
            if listing.exists() and "\n" in listing.read_text(encoding="utf-8"):
                os._exit(31)
            await asyncio.sleep(0.01)
        raise RuntimeError("真实 FFmpeg 未生成可清算媒体")

    async def synthetic_media(*args: str, **options: object) -> asyncio.subprocess.Process:
        listing = Path(args[args.index("-segment_list") + 1])
        command = [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-t",
            "20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "10",
            "-f",
            "segment",
            "-segment_time",
            "2",
            "-segment_list",
            str(listing),
            "-segment_list_type",
            "csv",
            args[-1],
        ]
        if os.name != "nt":
            command = [*args[:7], settings.ffmpeg_path, *command]
        process = await original(*command, **options)
        observers.append(asyncio.create_task(crash_after_media(listing)))
        return process

    # 只替换外部输入为真实 FFmpeg 合成媒体，保留录制器、所有权和数据库调用链。
    asyncio.create_subprocess_exec = synthetic_media
    settings.collect_danmaku = False
    settings.recording_stream_race_parallel = 1
    settings.min_free_disk_gb = 1
    await Recorder(identity, room.id, metadata_prepared=True).run()
    await asyncio.gather(*observers)
    raise RuntimeError("未到达预期录制中断点")


if __name__ == "__main__":
    asyncio.run(main())
