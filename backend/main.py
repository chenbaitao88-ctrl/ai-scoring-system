"""
评分系统 FastAPI 主入口
"""
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
import logging
import os

from database import init_db
from routers import teams, works, scores, export, judge_groups, plagiarism, task_books, scoring_forms, competitions, batch_scoring, evidence_packages, pipeline_tasks, scoring_pipeline, result_export

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化数据库
    logger.info("初始化数据库...")
    init_db()
    logger.info("数据库初始化完成")
    yield
    # 关闭时清理资源
    logger.info("应用关闭")


# 创建FastAPI应用
app = FastAPI(
    title="创意编程评分系统",
    description="通用创意编程作品人机协同评分系统",
    version="2.0.0",
    lifespan=lifespan
)

# CORS配置（允许前端访问，局域网部署时允许所有来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源（局域网内使用）
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 注册路由
app.include_router(teams.router, prefix="/api/teams", tags=["队伍管理"])
app.include_router(works.router, prefix="/api/works", tags=["作品管理"])
app.include_router(scores.router, prefix="/api/scores", tags=["评分管理"])
app.include_router(export.router, prefix="/api/export", tags=["数据导出"])
app.include_router(judge_groups.router, prefix="/api/judge-groups", tags=["评审组配置"])
app.include_router(plagiarism.router, prefix="/api/plagiarism", tags=["抄袭检测"])
app.include_router(task_books.router, tags=["任务书管理"])
app.include_router(scoring_forms.router, tags=["评审表管理"])
app.include_router(competitions.router, prefix="/api/competitions", tags=["比赛配置"])
app.include_router(batch_scoring.router, prefix="/api/batch-scoring", tags=["批量评分"])
# EvidencePackage 导入 API（Phase 11B-2b/2c/2d）；router 自带 /api/evidence-packages 前缀
app.include_router(evidence_packages.router)
# PipelineTask 流程 API（Phase 11C-3）；router 自带 /api/pipeline-tasks 前缀
app.include_router(pipeline_tasks.router)
app.include_router(scoring_pipeline.router)
# 权威结果导出判定 API（Phase 11F-3e-2）；router 自带 /api/result-export 前缀，须在 SPA catch-all 之前
app.include_router(result_export.router)


# EvidencePackage / PipelineTask 请求校验错误收口（11B-2d / 11C-3：REQUEST_VALIDATION_ERROR 稳定结构）
@app.exception_handler(RequestValidationError)
async def evidence_request_validation_handler(request: Request, exc: RequestValidationError):
    """仅对 /api/evidence-packages/* 与 /api/pipeline-tasks/* 返回冻结顶层错误结构；其他路径保持 FastAPI 默认校验响应。"""
    if request.url.path.startswith(("/api/evidence-packages", "/api/pipeline-tasks")):
        return JSONResponse(
            status_code=422,
            content={
                "code": "REQUEST_VALIDATION_ERROR",
                "message_key": "REQUEST_VALIDATION_ERROR",
                "retryable": False,
            },
        )
    return await request_validation_exception_handler(request, exc)


# 前端静态文件路径
DIST_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")


@app.get("/")
async def root():
    """根路径 - 返回前端页面"""
    if os.path.exists(DIST_DIR):
        return FileResponse(os.path.join(DIST_DIR, "index.html"))
    return {
        "message": "创意编程评分系统 API",
        "version": "2.0.0",
        "docs": "/docs",
        "warning": "前端未构建，请运行 npm run build"
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy"}


@app.get("/{path:path}")
async def serve_spa(path: str):
    """SPA路由支持"""
    if not os.path.exists(DIST_DIR):
        return {"error": "前端未构建"}

    # API路径不应该被SPA处理
    if path.startswith("api/"):
        return {"error": "Not Found"}

    file_path = os.path.join(DIST_DIR, path)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)
    # SPA fallback - return index.html for client-side routing
    return FileResponse(os.path.join(DIST_DIR, "index.html"))


def run_server():
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.environ.get("SCORING_HOST", "127.0.0.1"),
        port=int(os.environ.get("SCORING_PORT", "8000")),
        reload=False,  # 生产环境关闭reload
        log_level="info"
    )


if __name__ == "__main__":
    run_server()
