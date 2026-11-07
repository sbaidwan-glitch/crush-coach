# Crush Coach

A task tracker with an AI coach built into every task, for the MuddSub mechanical team at Harvey Mudd College. Members claim a task on our RoboSub vehicle, Crush; the coach answers questions from the team's own Google Drive and walks them through each stage; leads review work in the app; and the tracker updates itself as people work.

E190BO AI-Amplified Prototyping, Fall 2026 · Samara Baidwan

## Run it locally
1. `cp .env.example .env`
2. `docker compose up -d` (local Postgres)
3. Put secrets in your shell environment, never in a file: `GLASSROOM_API_KEY`, and `GOOGLE_APPLICATION_CREDENTIALS` pointing to a service-account key stored outside this repo.
4. `pip install -r requirements.txt`
5. `make dev`, then open http://localhost:8000

`make` lists every command. See `CLAUDE.md` for the project's rules and `PROCESS.md` for the design log.
