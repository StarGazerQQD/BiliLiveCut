"""Pipeline 阶段 Workers."""

from app.pipeline.workers.analyze import analyze_compute, commit_highlight, run_analyze  # noqa: F401
from app.pipeline.workers.publish import (  # noqa: F401
    commit_publish_result,
    execute_remote_upload,
    prepare_publish_attempt,
    run_publish,
)
from app.pipeline.workers.render import commit_render, render_compute, run_render  # noqa: F401
from app.pipeline.workers.transcribe import (  # noqa: F401
    commit_transcript,
    run_transcribe,
    transcribe_compute,
)

# 后向兼容别名
publish_compute = execute_remote_upload
commit_publish = commit_publish_result
