"""API route facade."""

from __future__ import annotations

from fastapi import APIRouter

from app.web.routers.ml import router as ml_router
from app.web.routers.analytics import router as analytics_router
from app.web.routers.auth import router as auth_router
from app.web.routers.candidates import router as candidates_router
from app.web.routers.clips import router as clips_router
from app.web.routers.container import router as container_router
from app.web.routers.dashboard import router as dashboard_router
from app.web.routers.llm import router as llm_router
from app.web.routers.logs import router as logs_router
from app.web.routers.media import router as media_router
from app.web.routers.metrics import router as metrics_router
from app.web.routers.progress import router as progress_router
from app.web.routers.rooms import router as rooms_router
from app.web.routers.schedules import router as schedules_router
from app.web.routers.segments import router as segments_router
from app.web.routers.tasks import router as tasks_router
from app.web.routers.topics import router as topics_router
from app.web.routers.trends import router as trends_router
from app.web.routers.variants import router as variants_router

router = APIRouter(prefix="/api")

router.include_router(dashboard_router)
router.include_router(rooms_router)
router.include_router(segments_router)
router.include_router(candidates_router)
router.include_router(clips_router)
router.include_router(container_router)
router.include_router(llm_router)
router.include_router(trends_router)
router.include_router(logs_router)
router.include_router(schedules_router)
router.include_router(progress_router)
router.include_router(media_router)
router.include_router(auth_router)
router.include_router(tasks_router)
router.include_router(topics_router)
router.include_router(variants_router)
router.include_router(ml_router)
router.include_router(analytics_router)
router.include_router(metrics_router)
