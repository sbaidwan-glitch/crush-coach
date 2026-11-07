import os
from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import models  # noqa: F401  (registers tables on SQLModel.metadata)

load_dotenv()

ALEMBIC_INI = Path(__file__).parent / "alembic.ini"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    # Railway hands out postgres://; SQLAlchemy needs the psycopg 3 driver named.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def _make_engine():
    url = database_url()
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory database
        return create_engine(url, **kwargs)
    return create_engine(url)


engine = _make_engine()


def alembic_config(target: Engine | None = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    if target is not None:
        config.attributes["engine"] = target
    return config


def run_migrations(target: Engine | None = None) -> None:
    """Bring the database to the latest schema (`alembic upgrade head`)."""
    command.upgrade(alembic_config(target), "head")


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
