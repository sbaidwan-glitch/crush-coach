import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import board
import coach
import db

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("crush_coach")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.run_migrations()
    except Exception as exc:  # noqa: BLE001  (any DB failure must not stop the app)
        # Log the error type only: the message can contain the connection URL.
        log.error(
            "Could not run database migrations (%s); starting anyway",
            type(exc).__name__,
        )
    yield


app = FastAPI(title="Crush Coach", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(board.router)
app.include_router(coach.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
