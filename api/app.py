import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent.utils.log_util import setup_admin_logging
from api.routes import router

setup_admin_logging()
logger = logging.getLogger("sql-agent-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Admin API starting")
    yield
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
    import uvicorn

    uvicorn.run(
        "api.app:app",
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", "8000")),
    )


if __name__ == "__main__":
    main()