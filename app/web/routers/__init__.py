"""API routers."""

from app.web.routers.analytics import router as analytics_router  # noqa: F401
from app.web.routers.auth import router as auth_router  # noqa: F401
from app.web.routers.candidates import router as candidates_router  # noqa: F401
from app.web.routers.clips import router as clips_router  # noqa: F401
from app.web.routers.container import router as container_router  # noqa: F401
from app.web.routers.dashboard import router as dashboard_router  # noqa: F401
from app.web.routers.llm import router as llm_router  # noqa: F401
from app.web.routers.logs import router as logs_router  # noqa: F401
from app.web.routers.media import router as media_router  # noqa: F401
from app.web.routers.metrics import router as metrics_router  # noqa: F401
from app.web.routers.progress import router as progress_router  # noqa: F401
from app.web.routers.rooms import router as rooms_router  # noqa: F401
from app.web.routers.schedules import router as schedules_router  # noqa: F401
from app.web.routers.segments import router as segments_router  # noqa: F401
from app.web.routers.tasks import router as tasks_router  # noqa: F401
from app.web.routers.topics import router as topics_router  # noqa: F401
from app.web.routers.trends import router as trends_router  # noqa: F401
from app.web.routers.variants import router as variants_router  # noqa: F401
