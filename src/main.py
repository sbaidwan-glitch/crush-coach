import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import admin
import auth
import board
import coach
import db
import decisions

STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
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
session_secret = os.environ.get("SESSION_SECRET")
if not session_secret:
    raise RuntimeError("SESSION_SECRET is not set (see .env.example)")
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="crush_coach_session",
    same_site="lax",
    https_only=os.environ.get("SESSION_COOKIE_SECURE", "false").casefold() == "true",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(auth.router)
app.include_router(board.router)
app.include_router(coach.router)
app.include_router(decisions.router)
app.include_router(admin.router)


@app.exception_handler(StarletteHTTPException)
async def html_for_forbidden(request: Request, exc: StarletteHTTPException):
    """A 403 is a page a person reads (leads only), not JSON."""
    if exc.status_code == 403:
        return templates.TemplateResponse(
            request,
            "forbidden.html",
            {"user": auth.session_user(request)},
            status_code=403,
        )
    return await http_exception_handler(request, exc)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"user": auth.session_user(request)}
    )
