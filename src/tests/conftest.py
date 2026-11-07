import os

# Must be set before `main`/`db` are imported: tests run on in-memory SQLite.
# Assigned (not setdefault) so a developer's .env can never point tests at Postgres.
os.environ["DATABASE_URL"] = "sqlite://"
