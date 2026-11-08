# Crush Coach

A task tracker with an AI coach built into every task, for the MuddSub mechanical team at Harvey Mudd College. Members claim a task on our RoboSub vehicle, Crush; the coach answers questions from the team's own Google Drive and walks them through each stage; leads review work in the app; and the tracker updates itself as people work.

E190BO AI-Amplified Prototyping, Fall 2026 · Samara Baidwan

## Run it locally
1. `cp .env.example .env`
2. `docker compose up -d` (local Postgres)
3. Put secrets in your shell environment (or the gitignored `.env`), never in a committed file: `GLASSROOM_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS` pointing to a service-account key stored outside this repo, and the sign-in variables below.
4. `pip install -r requirements.txt`
5. `make dev`, then open http://localhost:8000

`make` lists every command. See `CLAUDE.md` for the project's rules and `PROCESS.md` for the design log.

## Google sign-in

Create a Google OAuth 2.0 **Web application** client and configure these runtime
environment variables:

- `SESSION_SECRET`: a long random value used to sign session cookies. Generate
  one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`: the web client
  credentials from Google Cloud. Never commit either value.
- `ALLOWED_EMAILS`: optional comma-separated member addresses.
- `ALLOWED_DOMAIN`: optional single domain such as `g.hmc.edu`. An address may
  match either this domain or `ALLOWED_EMAILS`; if both are empty, nobody is
  admitted.
- `LEAD_EMAILS`: optional comma-separated subset of allowed addresses. These
  sessions are marked as leads for review workflows.
- `SESSION_COOKIE_SECURE`: leave `false` for local HTTP and set `true` in every
  HTTPS deployment.

Add `http://localhost:8000/auth/callback` as an authorized redirect URI on the
Google web client for local testing. Google requires an exact match, including
scheme, host, port, and path.

### Testing a PR preview

Each PR preview has its own hostname, so it also needs its own authorized
redirect URI. After the preview URL exists:

1. Add `https://<that-preview-host>/auth/callback` to the Google web client's
   authorized redirect URIs. Do not use a wildcard; Google will reject it.
2. Set `SESSION_SECRET`, the two Google OAuth credentials, the allowlist, and
   `SESSION_COOKIE_SECURE=true` in that preview's environment. Preview and
   production secrets should be managed by the hosting platform, not GitHub or
   the repository.
3. Open `https://<that-preview-host>/login`, sign in with a test account that is
   on the preview allowlist, confirm the name and **Log out** link appear, then
   verify an account outside the allowlist gets the **Not on the team list**
   page.

When the preview is removed, remove its redirect URI from the Google client to
keep the client configuration tidy.
