#!/usr/bin/env bash
# Starts a throwaway Postgres in Docker, talks to it with psql, then deletes it.
#
#   bash demos/hello_postgres.sh
#
# Uses its own container name and port 5433, so it cannot collide with the
# project's database from docker-compose.yml (port 5432). Nothing is kept.
set -euo pipefail

NAME=e190bo-hello-postgres
URL=postgresql://demo:demo@localhost:5433/demo

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup   # in case an earlier run was interrupted

echo "==> Starting Postgres (the first run downloads the image; give it a minute)"
docker run -d --name "$NAME" -p 5433:5432 \
  -e POSTGRES_USER=demo -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=demo \
  postgres:17 >/dev/null

echo "==> Waiting for it to accept connections"
for _ in $(seq 30); do
  psql "$URL" -c "select 1" >/dev/null 2>&1 && break
  sleep 1
done

echo "==> Querying it with psql"
psql "$URL" -c "select version();"
psql "$URL" <<'SQL'
create table idea (id serial primary key, title text);
insert into idea (title) values ('hello from psql');
select * from idea;
SQL

echo "✅ Docker, Postgres, and psql all work. Removing the container."
