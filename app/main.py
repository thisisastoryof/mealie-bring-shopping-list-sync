import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import init_db
from app.health import health
from app.logging_config import configure_logging, get_logger
from app.services.reconciler import poll_loop

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info(
        "startup",
        mealie=settings.mealie_base_url,
        bring_list=settings.bring_list_name,
        poll_interval=settings.poll_interval,
        on_complete=settings.on_complete,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(poll_loop(stop))
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        log.info("shutdown")


app = FastAPI(title="Mealie ⇄ Bring Shopping List Sync", lifespan=lifespan)


@app.get("/health")
async def get_health():
    payload = health.as_dict()
    code = 200 if payload["status"] != "degraded" else 503
    return JSONResponse(payload, status_code=code)
