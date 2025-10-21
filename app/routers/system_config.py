from fastapi import APIRouter, Depends, HTTPException, status
from typing import Any, Dict
import re

from app.core.config import settings
from app.routers.auth import get_current_user

router = APIRouter()

SENSITIVE_KEYS = {
    "MONGODB_PASSWORD",
    "REDIS_PASSWORD",
    "JWT_SECRET",
    "CSRF_SECRET",
    "STOCK_DATA_API_KEY",
    "REFRESH_TOKEN_EXPIRE_DAYS",  # not sensitive itself, but keep for completeness
}

MASK = "***"


def _mask_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in SENSITIVE_KEYS:
        return MASK
    # Mask URLs that may contain credentials
    if key in {"MONGO_URI", "REDIS_URL"} and isinstance(value, str):
        v = value
        # mongodb://user:pass@host:port/db?...
        v = re.sub(r"(mongodb://[^:/?#]+):([^@/]+)@", r"\1:***@", v)
        # redis://:pass@host:port/db
        v = re.sub(r"(redis://:)[^@/]+@", r"\1***@", v)
        return v
    return value


def _build_summary() -> Dict[str, Any]:
    raw = settings.model_dump()
    # Attach derived URLs
    raw["MONGO_URI"] = settings.MONGO_URI
    raw["REDIS_URL"] = settings.REDIS_URL

    summary: Dict[str, Any] = {}
    for k, v in raw.items():
        summary[k] = _mask_value(k, v)
    return summary


@router.get("/config/summary", tags=["system"], summary="配置概要（已屏蔽敏感项，需管理员）")
async def get_config_summary(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """
    返回当前生效的设置概要。敏感字段将以 *** 掩码显示。
    访问控制：需管理员身份。
    """
    if not current_user.get("is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return {"settings": _build_summary()}


@router.get("/config/validate", tags=["system"], summary="验证配置完整性")
async def validate_config():
    """
    验证系统配置的完整性和有效性。
    返回验证结果，包括缺少的配置项和无效的配置。

    验证内容：
    1. 环境变量配置（.env 文件）
    2. MongoDB 中存储的配置（大模型、数据源等）

    注意：此接口会先从 MongoDB 重载配置到环境变量，然后再验证。
    """
    from app.core.startup_validator import StartupValidator
    from app.core.config_bridge import bridge_config_to_env
    from app.services.config_service import config_service

    try:
        # 🔧 步骤1: 重载配置 - 从 MongoDB 读取配置并桥接到环境变量
        try:
            bridge_config_to_env()
            logger.info("✅ 配置已从 MongoDB 重载到环境变量")
        except Exception as e:
            logger.warning(f"⚠️  配置重载失败: {e}，将验证 .env 文件中的配置")

        # 🔍 步骤2: 验证环境变量配置
        validator = StartupValidator()
        env_result = validator.validate()

        # 🔍 步骤3: 验证 MongoDB 中的配置
        mongodb_validation = {
            "llm_configs": [],
            "data_source_configs": [],
            "warnings": []
        }

        try:
            system_config = await config_service.get_system_config()

            if system_config:
                # 验证大模型配置
                for llm_config in system_config.llm_configs:
                    validation_item = {
                        "provider": llm_config.provider,
                        "model_name": llm_config.model_name,
                        "enabled": llm_config.enabled,
                        "has_api_key": False,
                        "status": "未配置"
                    }

                    if llm_config.enabled:
                        # 检查 API Key 是否有效
                        if llm_config.api_key and validator._is_valid_api_key(llm_config.api_key):
                            validation_item["has_api_key"] = True
                            validation_item["status"] = "已配置"
                        else:
                            validation_item["status"] = "未配置或占位符"
                            mongodb_validation["warnings"].append(
                                f"大模型 {llm_config.model_name} 已启用但未配置有效的 API Key"
                            )
                    else:
                        validation_item["status"] = "已禁用"

                    mongodb_validation["llm_configs"].append(validation_item)

                # 验证数据源配置
                for ds_config in system_config.data_source_configs:
                    validation_item = {
                        "name": ds_config.name,
                        "type": ds_config.type,
                        "enabled": ds_config.enabled,
                        "has_api_key": False,
                        "status": "未配置"
                    }

                    if ds_config.enabled:
                        # 某些数据源不需要 API Key（如 AKShare）
                        if ds_config.type in ["akshare", "yahoo"]:
                            validation_item["has_api_key"] = True
                            validation_item["status"] = "已配置（无需密钥）"
                        elif ds_config.api_key and validator._is_valid_api_key(ds_config.api_key):
                            validation_item["has_api_key"] = True
                            validation_item["status"] = "已配置"
                        else:
                            validation_item["status"] = "未配置或占位符"
                            mongodb_validation["warnings"].append(
                                f"数据源 {ds_config.name} 已启用但未配置有效的 API Key"
                            )
                    else:
                        validation_item["status"] = "已禁用"

                    mongodb_validation["data_source_configs"].append(validation_item)
            else:
                mongodb_validation["warnings"].append("MongoDB 中没有找到系统配置")

        except Exception as e:
            logger.error(f"验证 MongoDB 配置失败: {e}")
            mongodb_validation["warnings"].append(f"MongoDB 配置验证失败: {str(e)}")

        # 合并验证结果
        return {
            "success": True,
            "data": {
                # 环境变量验证结果
                "env_validation": {
                    "success": env_result.success,
                    "missing_required": [
                        {"key": config.key, "description": config.description}
                        for config in env_result.missing_required
                    ],
                    "missing_recommended": [
                        {"key": config.key, "description": config.description}
                        for config in env_result.missing_recommended
                    ],
                    "invalid_configs": [
                        {"key": config.key, "error": config.description}
                        for config in env_result.invalid_configs
                    ],
                    "warnings": env_result.warnings
                },
                # MongoDB 配置验证结果
                "mongodb_validation": mongodb_validation,
                # 总体验证结果
                "success": env_result.success and len(mongodb_validation["warnings"]) == 0
            },
            "message": "配置验证完成"
        }
    except Exception as e:
        logger.error(f"配置验证失败: {e}", exc_info=True)
        return {
            "success": False,
            "data": None,
            "message": f"配置验证失败: {str(e)}"
        }
