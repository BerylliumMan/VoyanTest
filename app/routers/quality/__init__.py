# app/routers/quality/__init__.py
# 041 AI 质量智能：/api/quality 路由组。
from fastapi import APIRouter

from app.routers.quality import analysis, defect, flaky, impact, insights, requirements, review

router = APIRouter(prefix="/api/quality", tags=["quality"])

router.include_router(analysis.router)
router.include_router(review.router)
router.include_router(insights.router)
router.include_router(defect.router)
router.include_router(impact.router)
router.include_router(flaky.router)
router.include_router(requirements.router)

__all__ = ["router"]
