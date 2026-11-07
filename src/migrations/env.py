"""Alembic environment.

The database URL and engine come from db.py, so the CLI, the app's startup
migration, and the tests all share the same URL logic (including the
postgres:// -> postgresql+psycopg:// rewrite) and never read a URL from a file.
"""

from alembic import context
from sqlmodel import SQLModel

import db
import models  # noqa: F401  (registers tables on SQLModel.metadata)

# Deliberately no logging.config.fileConfig(): it would disable the app's
# existing loggers when migrations run inside the FastAPI startup.

config = context.config
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout (`alembic upgrade head --sql`) without connecting."""
    context.configure(
        url=db.database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A caller (db.run_migrations, tests) may hand in an engine; otherwise use
    # the app's. The shared in-memory SQLite engine used by tests only works if
    # we reuse it rather than build a second one.
    engine = config.attributes.get("engine") or db.engine
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
