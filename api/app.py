import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.log_util import setup_admin_logging
from api.routes import router
from api.sync_task import start_sync_task, stop_sync_task
from common.config import CLOSE_ENV
from common.env_utils import remove_proxy

setup_admin_logging()
logger = logging.getLogger("sql-agent-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Admin API starting")
    await start_sync_task()
    yield
    await stop_sync_task()
    from api.routes import get_retriever

    try:
        retriever = get_retriever()
        retriever.close()
    except Exception:
        pass
    logger.info("Admin API shutdown")


app = FastAPI(
    title="SQL Agent Admin API",
    description="向量库表结构管理接口（管理员专用）",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


def main():
    if CLOSE_ENV == "true":
        remove_proxy()

    import uvicorn

    uvicorn.run(
        "api.app:app",
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", "8000")),
    )


if __name__ == "__main__":
    main()