import os
import logging

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_admin(api_key: str = Security(_api_key_header)):
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_API_KEY 环境变量未配置，管理接口不可用",
        )
    if api_key != ADMIN_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="无效的 API Key",
        )
    return api_key
