"""
股票分析服务
将现有的TradingAgents分析功能包装成API服务
"""

import asyncio
import uuid
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable
from pathlib import Path
import sys

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# 初始化TradingAgents日志系统
from tradingagents.utils.logging_init import init_logging
init_logging()

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from app.services.simple_analysis_service import create_analysis_config, get_provider_by_model_name
from app.models.analysis import (
    AnalysisParameters, AnalysisResult, AnalysisTask, AnalysisBatch,
    AnalysisStatus, BatchStatus, SingleAnalysisRequest, BatchAnalysisRequest
)
from app.models.user import PyObjectId
from bson import ObjectId
from app.core.database import get_mongo_db
from app.core.redis_client import get_redis_service, RedisKeys
from app.services.queue_service import QueueService
from app.core.database import get_redis_client
from app.services.redis_progress_tracker import RedisProgressTracker
from app.services.config_provider import provider as config_provider
from app.services.queue import DEFAULT_USER_CONCURRENT_LIMIT, GLOBAL_CONCURRENT_LIMIT, VISIBILITY_TIMEOUT_SECONDS

import logging
logger = logging.getLogger(__name__)


class AnalysisService:
    """股票分析服务类"""
    
    def __init__(self):
        # 获取Redis客户端
        redis_client = get_redis_client()
        self.queue_service = QueueService(redis_client)
        self._trading_graph_cache = {}
        # 进度跟踪器缓存
        self._progress_trackers: Dict[str, RedisProgressTracker] = {}

    def _convert_user_id(self, user_id: str) -> PyObjectId:
        """将字符串用户ID转换为PyObjectId"""
        try:
            logger.info(f"🔄 开始转换用户ID: {user_id} (类型: {type(user_id)})")

            # 如果是admin用户，使用固定的ObjectId
            if user_id == "admin":
                # 使用固定的ObjectId作为admin用户ID
                admin_object_id = ObjectId("507f1f77bcf86cd799439011")
                logger.info(f"🔄 转换admin用户ID: {user_id} -> {admin_object_id}")
                return PyObjectId(admin_object_id)
            else:
                # 尝试将字符串转换为ObjectId
                object_id = ObjectId(user_id)
                logger.info(f"🔄 转换用户ID: {user_id} -> {object_id}")
                return PyObjectId(object_id)
        except Exception as e:
            logger.error(f"❌ 用户ID转换失败: {user_id} -> {e}")
            # 如果转换失败，生成一个新的ObjectId
            new_object_id = ObjectId()
            logger.warning(f"⚠️ 生成新的用户ID: {new_object_id}")
            return PyObjectId(new_object_id)
    
    def _get_trading_graph(self, config: Dict[str, Any]) -> TradingAgentsGraph:
        """获取或创建TradingAgents图实例（带缓存）- 与单股分析保持一致"""
        config_key = json.dumps(config, sort_keys=True)

        if config_key not in self._trading_graph_cache:
            # 直接使用完整配置，不再合并DEFAULT_CONFIG（因为create_analysis_config已经处理了）
            # 这与单股分析服务和web目录的方式一致
            self._trading_graph_cache[config_key] = TradingAgentsGraph(
                selected_analysts=config.get("selected_analysts", ["market", "fundamentals"]),
                debug=config.get("debug", False),
                config=config
            )

            logger.info(f"创建新的TradingAgents实例: {config.get('llm_provider', 'default')}")

        return self._trading_graph_cache[config_key]

    def _execute_analysis_sync_with_progress(self, task: AnalysisTask, progress_tracker: RedisProgressTracker) -> AnalysisResult:
        """同步执行分析任务（在线程池中运行，带进度跟踪）"""
        try:
            # 在线程中重新初始化日志系统
            from tradingagents.utils.logging_init import init_logging, get_logger
            init_logging()
            thread_logger = get_logger('analysis_thread')

            thread_logger.info(f"🔄 [线程池] 开始执行分析任务: {task.task_id} - {task.stock_code}")
            logger.info(f"🔄 [线程池] 开始执行分析任务: {task.task_id} - {task.stock_code}")

            # 环境检查
            progress_tracker.update_progress("🔧 检查环境配置")

            # 使用标准配置函数创建完整配置
            from app.core.unified_config import unified_config

            quick_model = getattr(task.parameters, 'quick_analysis_model', None) or unified_config.get_quick_analysis_model()
            deep_model = getattr(task.parameters, 'deep_analysis_model', None) or unified_config.get_deep_analysis_model()

            # 成本估算
            progress_tracker.update_progress("💰 预估分析成本")

            # 根据模型名称动态查找供应商（同步版本）
            llm_provider = "dashscope"  # 默认使用dashscope

            # 参数配置
            progress_tracker.update_progress("⚙️ 配置分析参数")

            # 使用标准配置函数创建完整配置
            from app.services.simple_analysis_service import create_analysis_config
            config = create_analysis_config(
                research_depth=task.parameters.research_depth,
                selected_analysts=task.parameters.selected_analysts or ["market", "fundamentals"],
                quick_model=quick_model,
                deep_model=deep_model,
                llm_provider=llm_provider,
                market_type=getattr(task.parameters, 'market_type', "A股")
            )

            # 启动引擎
            progress_tracker.update_progress("🚀 初始化AI分析引擎")

            # 获取TradingAgents实例
            trading_graph = self._get_trading_graph(config)

            # 执行分析
            from datetime import timezone
            start_time = datetime.now(timezone.utc)
            analysis_date = task.parameters.analysis_date or datetime.now().strftime("%Y-%m-%d")

            # 创建进度回调函数
            def progress_callback(message: str):
                progress_tracker.update_progress(message)

            # 调用现有的分析方法（同步调用，传递进度回调）
            _, decision = trading_graph.propagate(task.stock_code, analysis_date, progress_callback)

            execution_time = (datetime.now(timezone.utc) - start_time).total_seconds()

            # 生成报告
            progress_tracker.update_progress("📊 生成分析报告")

            # 构建结果
            result = AnalysisResult(
                analysis_id=str(uuid.uuid4()),
                summary=decision.get("summary", ""),
                recommendation=decision.get("recommendation", ""),
                confidence_score=decision.get("confidence_score", 0.0),
                risk_level=decision.get("risk_level", "中等"),
                key_points=decision.get("key_points", []),
                detailed_analysis=decision,
                execution_time=execution_time,
                tokens_used=decision.get("tokens_used", 0)
            )

            logger.info(f"✅ [线程池] 分析任务完成: {task.task_id} - 耗时{execution_time:.2f}秒")
            return result

        except Exception as e:
            logger.error(f"❌ [线程池] 执行分析任务失败: {task.task_id} - {e}")
            raise

    def _execute_analysis_sync(self, task: AnalysisTask) -> AnalysisResult:
        """同步执行分析任务（在线程池中运行）"""
        try:
            logger.info(f"🔄 [线程池] 开始执行分析任务: {task.task_id} - {task.stock_code}")

            # 使用标准配置函数创建完整配置
            from app.core.unified_config import unified_config

            quick_model = getattr(task.parameters, 'quick_analysis_model', None) or unified_config.get_quick_analysis_model()
            deep_model = getattr(task.parameters, 'deep_analysis_model', None) or unified_config.get_deep_analysis_model()

            # 根据模型名称动态查找供应商（同步版本）
            llm_provider = "dashscope"  # 默认使用dashscope

            # 使用标准配置函数创建完整配置
            from app.services.simple_analysis_service import create_analysis_config
            config = create_analysis_config(
                research_depth=task.parameters.research_depth,
                selected_analysts=task.parameters.selected_analysts or ["market", "fundamentals"],
                quick_model=quick_model,
                deep_model=deep_model,
                llm_provider=llm_provider,
                market_type=getattr(task.parameters, 'market_type', "A股")
            )

            # 获取TradingAgents实例
            trading_graph = self._get_trading_graph(config)

            # 执行分析
            from datetime import timezone
            start_time = datetime.now(timezone.utc)
            analysis_date = task.parameters.analysis_date or datetime.now().strftime("%Y-%m-%d")

            # 调用现有的分析方法（同步调用）
            _, decision = trading_graph.propagate(task.stock_code, analysis_date)

            execution_time = (datetime.now(timezone.utc) - start_time).total_seconds()

            # 构建结果
            result = AnalysisResult(
                analysis_id=str(uuid.uuid4()),
                summary=decision.get("summary", ""),
                recommendation=decision.get("recommendation", ""),
                confidence_score=decision.get("confidence_score", 0.0),
                risk_level=decision.get("risk_level", "中等"),
                key_points=decision.get("key_points", []),
                detailed_analysis=decision,
                execution_time=execution_time,
                tokens_used=decision.get("tokens_used", 0)
            )

            logger.info(f"✅ [线程池] 分析任务完成: {task.task_id} - 耗时{execution_time:.2f}秒")
            return result

        except Exception as e:
            logger.error(f"❌ [线程池] 执行分析任务失败: {task.task_id} - {e}")
            raise

    async def _execute_single_analysis_async(self, task: AnalysisTask):
        """异步执行单股分析任务（在后台运行，不阻塞主线程）"""
        progress_tracker = None
        try:
            logger.info(f"🔄 开始执行分析任务: {task.task_id} - {task.stock_code}")

            # 创建进度跟踪器
            progress_tracker = RedisProgressTracker(
                task_id=task.task_id,
                analysts=task.parameters.selected_analysts or ["market", "fundamentals"],
                research_depth=task.parameters.research_depth or "标准",
                llm_provider="dashscope"
            )

            # 缓存进度跟踪器
            self._progress_trackers[task.task_id] = progress_tracker

            # 初始化进度
            progress_tracker.update_progress("🚀 开始股票分析")
            await self._update_task_status_with_tracker(task.task_id, AnalysisStatus.PROCESSING, progress_tracker)

            # 在线程池中执行分析，避免阻塞事件循环
            import asyncio
            import concurrent.futures

            loop = asyncio.get_event_loop()

            # 使用线程池执行器运行同步的分析代码
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = await loop.run_in_executor(
                    executor,
                    self._execute_analysis_sync_with_progress,
                    task,
                    progress_tracker
                )

            # 标记完成
            progress_tracker.mark_completed("✅ 分析完成")
            await self._update_task_status_with_tracker(task.task_id, AnalysisStatus.COMPLETED, progress_tracker, result)

            logger.info(f"✅ 分析任务完成: {task.task_id}")

        except Exception as e:
            logger.error(f"❌ 分析任务失败: {task.task_id} - {e}")

            # 标记失败
            if progress_tracker:
                progress_tracker.mark_failed(str(e))
                await self._update_task_status_with_tracker(task.task_id, AnalysisStatus.FAILED, progress_tracker)
            else:
                await self._update_task_status(task.task_id, AnalysisStatus.FAILED, 0, str(e))
        finally:
            # 清理进度跟踪器缓存
            if task.task_id in self._progress_trackers:
                del self._progress_trackers[task.task_id]

    async def submit_single_analysis(
        self,
        user_id: str,
        request: SingleAnalysisRequest
    ) -> Dict[str, Any]:
        """提交单股分析任务"""
        try:
            logger.info(f"📝 开始提交单股分析任务")
            logger.info(f"👤 用户ID: {user_id} (类型: {type(user_id)})")
            logger.info(f"📊 股票代码: {request.stock_code}")
            logger.info(f"⚙️ 分析参数: {request.parameters}")

            # 生成任务ID
            task_id = str(uuid.uuid4())
            logger.info(f"🆔 生成任务ID: {task_id}")

            # 转换用户ID
            converted_user_id = self._convert_user_id(user_id)
            logger.info(f"🔄 转换后的用户ID: {converted_user_id} (类型: {type(converted_user_id)})")

            # 创建分析任务
            logger.info(f"🏗️ 开始创建AnalysisTask对象...")

            # 读取合并后的系统设置（ENV 优先 → DB），用于填充模型与并发/超时配置
            try:
                effective_settings = await config_provider.get_effective_system_settings()
            except Exception:
                effective_settings = {}

            # 填充分析参数中的模型（若请求未显式提供）
            params = request.parameters or AnalysisParameters()
            if not getattr(params, 'quick_analysis_model', None):
                params.quick_analysis_model = effective_settings.get("quick_analysis_model", "qwen-turbo")
            if not getattr(params, 'deep_analysis_model', None):
                params.deep_analysis_model = effective_settings.get("deep_analysis_model", "qwen-max")

            # 应用系统级并发与可见性超时（若提供）
            try:
                self.queue_service.user_concurrent_limit = int(effective_settings.get("max_concurrent_tasks", DEFAULT_USER_CONCURRENT_LIMIT))
                self.queue_service.global_concurrent_limit = int(effective_settings.get("max_concurrent_tasks", GLOBAL_CONCURRENT_LIMIT))
                self.queue_service.visibility_timeout = int(effective_settings.get("default_analysis_timeout", VISIBILITY_TIMEOUT_SECONDS))
            except Exception:
                # 使用默认值即可
                pass

            task = AnalysisTask(
                task_id=task_id,
                user_id=converted_user_id,
                stock_code=request.stock_code,
                parameters=params,
                status=AnalysisStatus.PENDING
            )
            logger.info(f"✅ AnalysisTask对象创建成功")

            # 保存任务到数据库
            logger.info(f"💾 开始保存任务到数据库...")
            db = get_mongo_db()
            task_dict = task.model_dump(by_alias=True)
            logger.info(f"📄 任务字典: {task_dict}")
            await db.analysis_tasks.insert_one(task_dict)
            logger.info(f"✅ 任务已保存到数据库")

            # 单股分析：直接在后台执行（不阻塞API响应）
            logger.info(f"🚀 开始在后台执行分析任务...")

            # 创建后台任务，不等待完成
            import asyncio
            background_task = asyncio.create_task(
                self._execute_single_analysis_async(task)
            )

            # 不等待任务完成，让它在后台运行
            logger.info(f"✅ 后台任务已启动，任务ID: {task_id}")

            logger.info(f"🎉 单股分析任务提交完成: {task_id} - {request.stock_code}")

            return {
                "task_id": task_id,
                "stock_code": request.stock_code,
                "status": AnalysisStatus.PENDING,
                "message": "任务已在后台启动"
            }
            
        except Exception as e:
            logger.error(f"提交单股分析任务失败: {e}")
            raise
    
    async def submit_batch_analysis(
        self, 
        user_id: str, 
        request: BatchAnalysisRequest
    ) -> Dict[str, Any]:
        """提交批量分析任务"""
        try:
            # 生成批次ID
            batch_id = str(uuid.uuid4())
            
            # 转换用户ID
            converted_user_id = self._convert_user_id(user_id)

            # 读取系统设置，填充模型参数并应用并发/超时配置
            try:
                effective_settings = await config_provider.get_effective_system_settings()
            except Exception:
                effective_settings = {}

            params = request.parameters or AnalysisParameters()
            if not getattr(params, 'quick_analysis_model', None):
                params.quick_analysis_model = effective_settings.get("quick_analysis_model", "qwen-turbo")
            if not getattr(params, 'deep_analysis_model', None):
                params.deep_analysis_model = effective_settings.get("deep_analysis_model", "qwen-max")

            try:
                self.queue_service.user_concurrent_limit = int(effective_settings.get("max_concurrent_tasks", DEFAULT_USER_CONCURRENT_LIMIT))
                self.queue_service.global_concurrent_limit = int(effective_settings.get("max_concurrent_tasks", GLOBAL_CONCURRENT_LIMIT))
                self.queue_service.visibility_timeout = int(effective_settings.get("default_analysis_timeout", VISIBILITY_TIMEOUT_SECONDS))
            except Exception:
                pass

            # 创建批次记录
            batch = AnalysisBatch(
                batch_id=batch_id,
                user_id=converted_user_id,
                title=request.title,
                description=request.description,
                total_tasks=len(request.stock_codes),
                parameters=params,
                status=BatchStatus.PENDING
            )

            # 创建任务列表
            tasks = []
            for stock_code in request.stock_codes:
                task_id = str(uuid.uuid4())
                task = AnalysisTask(
                    task_id=task_id,
                    batch_id=batch_id,
                    user_id=converted_user_id,
                    stock_code=stock_code,
                    parameters=batch.parameters,
                    status=AnalysisStatus.PENDING
                )
                tasks.append(task)
            
            # 保存到数据库
            db = get_mongo_db()
            await db.analysis_batches.insert_one(batch.dict(by_alias=True))
            await db.analysis_tasks.insert_many([task.dict(by_alias=True) for task in tasks])
            
            # 提交任务到队列
            for task in tasks:
                # 准备队列参数（直接传递分析参数，不嵌套）
                queue_params = task.parameters.dict() if task.parameters else {}

                # 添加任务元数据
                queue_params.update({
                    "task_id": task.task_id,
                    "stock_code": task.stock_code,
                    "user_id": str(task.user_id),
                    "batch_id": task.batch_id,
                    "created_at": task.created_at.isoformat() if task.created_at else None
                })

                # 调用队列服务
                await self.queue_service.enqueue_task(
                    user_id=str(converted_user_id),
                    symbol=task.stock_code,
                    params=queue_params,
                    batch_id=task.batch_id
                )
            
            logger.info(f"批量分析任务已提交: {batch_id} - {len(tasks)}个股票")
            
            return {
                "batch_id": batch_id,
                "total_tasks": len(tasks),
                "status": BatchStatus.PENDING,
                "message": f"已提交{len(tasks)}个分析任务到队列"
            }
            
        except Exception as e:
            logger.error(f"提交批量分析任务失败: {e}")
            raise
    
    async def execute_analysis_task(
        self, 
        task: AnalysisTask,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ) -> AnalysisResult:
        """执行单个分析任务"""
        try:
            logger.info(f"开始执行分析任务: {task.task_id} - {task.stock_code}")
            
            # 更新任务状态
            await self._update_task_status(task.task_id, AnalysisStatus.PROCESSING, 0)
            
            if progress_callback:
                progress_callback(10, "初始化分析引擎...")
            
            # 使用标准配置函数创建完整配置 - 与单股分析保持一致
            from app.core.unified_config import unified_config

            quick_model = getattr(task.parameters, 'quick_analysis_model', None) or unified_config.get_quick_analysis_model()
            deep_model = getattr(task.parameters, 'deep_analysis_model', None) or unified_config.get_deep_analysis_model()

            # 根据模型名称动态查找供应商
            llm_provider = await get_provider_by_model_name(quick_model)

            # 使用标准配置函数创建完整配置
            config = create_analysis_config(
                research_depth=task.parameters.research_depth,
                selected_analysts=task.parameters.selected_analysts or ["market", "fundamentals"],
                quick_model=quick_model,
                deep_model=deep_model,
                llm_provider=llm_provider,
                market_type=getattr(task.parameters, 'market_type', "A股")
            )
            
            if progress_callback:
                progress_callback(30, "创建分析图...")
            
            # 获取TradingAgents实例
            trading_graph = self._get_trading_graph(config)
            
            if progress_callback:
                progress_callback(50, "执行股票分析...")
            
            # 执行分析
            start_time = datetime.utcnow()
            analysis_date = task.parameters.analysis_date or datetime.now().strftime("%Y-%m-%d")
            
            # 调用现有的分析方法
            _, decision = trading_graph.propagate(task.stock_code, analysis_date)
            
            execution_time = (datetime.utcnow() - start_time).total_seconds()
            
            if progress_callback:
                progress_callback(80, "处理分析结果...")
            
            # 构建结果
            result = AnalysisResult(
                analysis_id=str(uuid.uuid4()),
                summary=decision.get("summary", ""),
                recommendation=decision.get("recommendation", ""),
                confidence_score=decision.get("confidence_score", 0.0),
                risk_level=decision.get("risk_level", "中等"),
                key_points=decision.get("key_points", []),
                detailed_analysis=decision,
                execution_time=execution_time,
                tokens_used=decision.get("tokens_used", 0)
            )
            
            if progress_callback:
                progress_callback(100, "分析完成")
            
            # 更新任务状态
            await self._update_task_status(task.task_id, AnalysisStatus.COMPLETED, 100, result)
            
            logger.info(f"分析任务完成: {task.task_id} - 耗时{execution_time:.2f}秒")
            
            return result
            
        except Exception as e:
            logger.error(f"执行分析任务失败: {task.task_id} - {e}")
            
            # 更新任务状态为失败
            error_result = AnalysisResult(error_message=str(e))
            await self._update_task_status(task.task_id, AnalysisStatus.FAILED, 0, error_result)
            
            raise
    
    async def _update_task_status(
        self,
        task_id: str,
        status: AnalysisStatus,
        progress: int,
        result: Optional[AnalysisResult] = None,
    ) -> None:
        """更新任务状态（委托至拆分的工具函数）"""
        try:
            from app.services.analysis.status_update_utils import perform_update_task_status
            await perform_update_task_status(task_id, status, progress, result)
        except Exception as e:
            logger.error(f"更新任务状态失败: {task_id} - {e}")

    async def _update_task_status_with_tracker(
        self,
        task_id: str,
        status: AnalysisStatus,
        progress_tracker: RedisProgressTracker,
        result: Optional[AnalysisResult] = None,
    ) -> None:
        """使用进度跟踪器更新任务状态（委托至拆分的工具函数）"""
        try:
            from app.services.analysis.status_update_utils import perform_update_task_status_with_tracker
            await perform_update_task_status_with_tracker(task_id, status, progress_tracker, result)
        except Exception as e:
            logger.error(f"更新任务状态失败: {task_id} - {e}")

    async def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态"""
        try:
            # 先检查内存中的进度跟踪器
            if task_id in self._progress_trackers:
                progress_tracker = self._progress_trackers[task_id]
                progress_data = progress_tracker.to_dict()

                # 从数据库获取任务基本信息
                db = get_mongo_db()
                task = await db.analysis_tasks.find_one({"task_id": task_id})

                if task:
                    # 合并数据库信息和进度跟踪器信息
                    return {
                        "task_id": task_id,
                        "user_id": task.get("user_id"),
                        "stock_code": task.get("stock_symbol"),
                        "status": progress_data["status"],
                        "progress": progress_data["progress"],
                        "current_step": progress_data["current_step"],
                        "message": progress_data["message"],
                        "elapsed_time": progress_data["elapsed_time"],
                        "remaining_time": progress_data["remaining_time"],
                        "estimated_total_time": progress_data.get("estimated_total_time", 0),
                        "steps": progress_data["steps"],
                        "start_time": progress_data["start_time"],
                        "end_time": None,
                        "last_update": progress_data["last_update"],
                        "parameters": task.get("parameters", {}),
                        "execution_time": None,
                        "tokens_used": None,
                        "result_data": task.get("result"),
                        "error_message": None
                    }

            # 从Redis缓存获取
            redis_service = get_redis_service()
            progress_key = RedisKeys.TASK_PROGRESS.format(task_id=task_id)
            cached_status = await redis_service.get_json(progress_key)

            if cached_status:
                return cached_status

            # 从数据库获取
            db = get_mongo_db()
            task = await db.analysis_tasks.find_one({"task_id": task_id})

            if task:
                # 计算已用时间
                elapsed_time = 0
                remaining_time = 0
                estimated_total_time = 0

                if task.get("started_at"):
                    from datetime import datetime
                    start_time = task.get("started_at")
                    if task.get("completed_at"):
                        # 任务已完成
                        elapsed_time = (task.get("completed_at") - start_time).total_seconds()
                        estimated_total_time = elapsed_time  # 已完成任务的总时长就是已用时间
                        remaining_time = 0
                    else:
                        # 任务进行中
                        elapsed_time = (datetime.utcnow() - start_time).total_seconds()
                        # 采用web目录的时间估算逻辑
                        progress = task.get("progress", 0) / 100

                        # 优先使用默认预估时间
                        estimated_total_time = 300  # 默认5分钟
                        remaining_time = max(0, estimated_total_time - elapsed_time)

                        # 如果已经超过预估时间，根据当前进度动态调整
                        if remaining_time <= 0 and progress > 0:
                            estimated_total_time = elapsed_time / progress
                            remaining_time = max(0, estimated_total_time - elapsed_time)

                return {
                    "task_id": task_id,
                    "status": task.get("status"),
                    "progress": task.get("progress", 0),
                    "current_step": task.get("current_step", ""),
                    "message": task.get("message", ""),
                    "elapsed_time": elapsed_time,
                    "remaining_time": remaining_time,
                    "estimated_total_time": estimated_total_time,
                    "start_time": task.get("started_at").isoformat() if task.get("started_at") else None,
                    "updated_at": task.get("updated_at", "").isoformat() if task.get("updated_at") else None,
                    "result_data": task.get("result")
                }

            return None

        except Exception as e:
            logger.error(f"获取任务状态失败: {task_id} - {e}")
            return None
    
    async def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        try:
            # 更新任务状态
            await self._update_task_status(task_id, AnalysisStatus.CANCELLED, 0)
            
            # 从队列中移除（如果还在队列中）
            await self.queue_service.remove_task(task_id)
            
            logger.info(f"任务已取消: {task_id}")
            return True
            
        except Exception as e:
            logger.error(f"取消任务失败: {task_id} - {e}")
            return False


# 全局分析服务实例（延迟初始化）
analysis_service: Optional[AnalysisService] = None


def get_analysis_service() -> AnalysisService:
    """获取分析服务实例（延迟初始化）"""
    global analysis_service
    if analysis_service is None:
        analysis_service = AnalysisService()
    return analysis_service
