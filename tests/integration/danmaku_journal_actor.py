from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from app.db.entities import Danmaku
from app.recording.danmaku_evidence import CaptureInterval, DanmakuEvidence
from app.recording.danmaku_journal import DanmakuJournal, drain_journal, journal_path


def main() -> None:
    mode = sys.argv[1]
    journal = DanmakuJournal(1)
    now = datetime.now(UTC)
    journal.append_observation(
        DanmakuEvidence(status="available", intervals=[CaptureInterval(start=now)], confirmed_until=now)
    )
    journal.append_messages([Danmaku(session_id=1, room_id=123, ts=now, content="same-event") for _ in range(2)])
    if mode == "committed":
        assert drain_journal(1)
    if mode == "torn":
        descriptor = os.open(journal_path(1), os.O_WRONLY | os.O_APPEND)
        os.write(descriptor, b'{"kind":"message","content":"unfinished')
    os._exit(31)


if __name__ == "__main__":
    main()
