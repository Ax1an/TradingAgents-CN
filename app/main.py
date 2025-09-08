"""
TradingAgents-CN v0.1.16 FastAPI Backend
主应用程序入口
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
import uvicorn
import logging
import time
from contextlib import asynccontextmanager
import asyncio

from app.core.config import settings
from app.core.database import init_db, close_db
from app.core.logging_config import setup_logging
from app.routers import auth, analysis, screening, queue, sse, health, favorites, config, reports, database, operation_logs
from app.routers import sync as sync_router, multi_source_sync
from app.services.basics_sync_service import get_basics_sync_service
from app.middleware.operation_log_middleware import OperationLogMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化
    setup_logging()
    logger = logging.getLogger("app.main")
    await init_db()
    logger.info("TradingAgents FastAPI backend started")

    # 启动每日定时任务：可配置
    scheduler: AsyncIOScheduler | None = None
    try:
        from croniter import croniter
    except Exception:
        croniter = None  # 可选依赖
    try:
        scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)
        service = get_basics_sync_service()
        # 立即在启动后尝试一次（不阻塞）
        asyncio.create_task(service.run_full_sync(force=False))

        # 配置调度：优先使用 CRON，其次使用 HH:MM
        if settings.SYNC_STOCK_BASICS_ENABLED:
            if settings.SYNC_STOCK_BASICS_CRON:
                # 如果提供了cron表达式
                scheduler.add_job(
                    lambda: asyncio.create_task(service.run_full_sync()),
                    CronTrigger.from_crontab(settings.SYNC_STOCK_BASICS_CRON, timezone=settings.TIMEZONE)
                )
                logger.info(f"📅 Stock basics sync scheduled by CRON: {settings.SYNC_STOCK_BASICS_CRON} ({settings.TIMEZONE})")
            else:
                hh, mm = (settings.SYNC_STOCK_BASICS_TIME or "06:30").split(":")
                scheduler.add_job(
                    lambda: asyncio.create_task(service.run_full_sync()),
                    CronTrigger(hour=int(hh), minute=int(mm), timezone=settings.TIMEZONE)
                )
                logger.info(f"📅 Stock basics sync scheduled daily at {settings.SYNC_STOCK_BASICS_TIME} ({settings.TIMEZONE})")

        scheduler.start()
    except Exception as e:
        logger.warning(f"Failed to start scheduler: {e}")

    try:
        yield
    finally:
        # 关闭时清理
        if scheduler:
            try:
                scheduler.shutdown(wait=False)
                logger.info("🛑 Scheduler stopped")
            except Exception as e:
                logger.warning(f"Scheduler shutdown error: {e}")
        await close_db()
        logger.info("TradingAgents FastAPI backend stopped")


# 创建FastAPI应用
app = FastAPI(
    title="TradingAgents-CN API",
    description="股票分析与批量队列系统 API",
    version="0.1.16",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    lifespan=lifespan
)

# 安全中间件
if not settings.DEBUG:
    app.add_middleware(
        TrustedHostMiddleware, 
        allowed_hosts=settings.ALLOWED_HOSTS
    )

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 操作日志中间件
app.add_middleware(OperationLogMiddleware)


# 请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()

    # 跳过健康检查和静态文件请求的日志
    if request.url.path in ["/health", "/favicon.ico"] or request.url.path.startswith("/static"):
        response = await call_next(request)
        return response

    # 使用webapi logger记录请求
    logger = logging.getLogger("webapi")
    logger.info(f"🔄 {request.method} {request.url.path} - 开始处理")

    response = await call_next(request)
    process_time = time.time() - start_time

    # 记录请求完成
    status_emoji = "✅" if response.status_code < 400 else "❌"
    logger.info(f"{status_emoji} {request.method} {request.url.path} - 状态: {response.status_code} - 耗时: {process_time:.3f}s")

    return response


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "Internal server error occurred",
                "request_id": getattr(request.state, "request_id", None)
            }
        }
    )


# 测试端点 - 验证中间件是否工作
@app.get("/api/test-log")
async def test_log():
    """测试日志中间件是否工作"""
    print("🧪 测试端点被调用 - 这条消息应该出现在控制台")
    return {"message": "测试成功", "timestamp": time.time()}

# 注册路由
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(auth.router, prefix="/api/auth", tags=["authentication"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["analysis"])
app.include_router(reports.router, tags=["reports"])
app.include_router(screening.router, prefix="/api/screening", tags=["screening"])
app.include_router(queue.router, prefix="/api/queue", tags=["queue"])
app.include_router(favorites.router, prefix="/api", tags=["favorites"])
app.include_router(config.router, prefix="/api", tags=["config"])
app.include_router(database.router, prefix="/api/system", tags=["database"])
app.include_router(operation_logs.router, prefix="/api/system", tags=["operation_logs"])
app.include_router(sse.router, prefix="/api/stream", tags=["streaming"])
app.include_router(sync_router.router)
app.include_router(multi_source_sync.router)


@app.get("/")
async def root():
    """根路径，返回API信息"""
    print("🏠 根路径被访问")
    return {
        "name": "TradingAgents-CN API",
        "version": "0.1.16",
        "status": "running",
        "docs_url": "/docs" if settings.DEBUG else None
    }


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
        reload_dirs=["app"] if settings.DEBUG else None,
        reload_excludes=[
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "*.pyd",
            ".git",
            ".pytest_cache",
            "*.log",
            "*.tmp"
        ] if settings.DEBUG else None,
        reload_includes=["*.py"] if settings.DEBUG else None
    )