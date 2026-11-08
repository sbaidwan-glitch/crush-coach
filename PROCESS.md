# Process log: Crush Coach

Newest entries at the bottom. Entries from here on are drafted with the /reflect skill. Photos and screenshots live in `process/`.

## 28 Oct 2026: Ideation and choosing a problem
**What I did:** Brainstormed project ideas that would be useful to me as Makerspace head steward and MuddSub mechanical co-lead. Main candidates: a live machine-status board for the Makerspace, a Makerspace manual assistant, a training tracker, an interactive Makerspace map with a project planner, and a MuddSub task coach. Kept them as two separate project ideas (Makerspace map vs. MuddSub coach) and compared them against the project brief.

**What I learned:** The Makerspace map risked failing the "take the model out and it loses its point" test, since a clickable map works without an LLM. The MuddSub coach needed the model for coaching and digging through Drive docs, and the Google Drive API gave it a natural new API. The MuddSub task tracker showed 29 of 31 tasks with no owner.

**What I changed:** Chose the MuddSub coach. Members message me when stuck and I miss or answer late; nobody opens the tracker because it's tedious.

## 28 Oct 2026: The latch problem
**What I did:** While working on Crush's lid, I found the latches we're using are different from the ones on Alfie. I had to physically compare the lid's latch holes to the side-panel CAD, and only found the current latch after unsuppressing COTS components in SolidWorks (the assembly was saved under one member's path, so COTS parts were suppressed). Searched the Drive for "latch": the only readable result was a 2023 drawing from Alfie.

**What I learned:** The team's latest decisions often live only in CAD or are buried in long notebooks (e.g., the gasket reasoning), and nothing records status (is the custom gasket ordered?). Digging up legacy information is a big part of my job, and I can't easily defend decisions I didn't make.

**What I changed:** Added a decision record to the design: approved decisions are saved with reasoning and sources, the coach trusts them over raw Drive search, and flags stale docs that conflict.

## 28 Oct 2026: Proposal and Drive API proof
**What I did:** Wrote the proposal. Explored concept screens (Quest Board, Build the Sub, Coach Thread) and two looks (pixel arcade, sonar). Set up the Google Cloud project `crush-coach`, a service account with Viewer access to the Mechanical shared-drive folder, and ran a test script that listed the folder.

**What I learned:** The first run returned nothing because shared drives need `supportsAllDrives` and `includeItemsFromAllDrives`. With those, the robot account could also see the legacy folders (25-26 References, Older References).

**What I changed:** Pasted the real response into the proposal; noted the shared-drive flags as a rule for the app.

## 3 Nov 2026: First interviews
**What I did:** Talked to Katie (former member of another tech club, in person, shown the gamified mockup), Kayla (new MuddSub mechanical member, by text, shown screenshots), and a MuddSub lead (voice memo, shown screenshots).

**What I learned:**
- Kayla got stuck making a process router and "lowkey didn't get unstuck"; she waited until the next meeting. What stops her claiming tasks is not understanding what a task entails. "Highkey the drive is confusing."
- The lead said members mostly ask "is there a document for this?", not "how do I do this?" Drive search "is not really functional": it needs exact keywords and returns a mix of random files.
- Katie's club docs lacked design rationale; she worries about "little stuff" being lost when leads graduate. She liked tracking progress but not XP: points don't represent what someone did.
- Two of three didn't want XP. All three wanted a way to ask questions of the Drive. The lead trusts AI for advice and big documents, never for major decisions.

**What I changed:** Cut XP and points; kept progress (Crush's build progress, completed tasks). Shifted the center of the product to finding answers in the Drive, with the coach inside each task. Kept the tracker as the backbone, updated automatically as people work.

## 6 Nov 2026: Repo setup
**What I did:** Created the repo from the course template, removed lab-only files, and set up CLAUDE.md, permission rules that block the agent from reading secret files, and this log. Stored the Glassroom key in macOS Keychain and loaded it into the shell environment, so it is never in a file. Chose to work locally with Conductor instead of Codespaces.

**What I learned:** _(add)_

**What I changed:** _(add)_

## 9 Nov 2026: Prototype presentation
**What I did:** Presented the problem, interviews, and prototypes. Asked my pod what progress signal would make them open a club tracker weekly, and where they'd look for a saved decision a year later.

**What I learned:** _(add pod and instructor feedback here)_

**What I changed:** _(add)_

## 2026-11-07 — Deploying to Railway, and moving to Alembic first

**Stage:** Building

**What I did.** I deployed the app on Railway with a Postgres database. The app gets its database address from a `DATABASE_URL` reference variable, so the password stays inside Railway and never ends up in a file. Before deploying, I switched the database setup from `create_all` to Alembic migrations, which work like revision control for the database's tables. Claude's review of my first PR (#1) raised the idea. I added it as a review comment and had the agent do it as its own PR (#2). It also added a test that fails if the models and migrations disagree.

**The rest of the build since my last entry.** These are the pieces that went out with PR #1 and made the deploy possible:
- A FastAPI app with a `/health` route, an index page and the `Task` model (f3329e3).
- Tests for the health route and index page that run on SQLite (7fc2eab).
- A Dockerfile that starts the app on `$PORT` (b8d7800).
- CI that runs ruff and pytest on every push and PR (71962b2).
- A change so every command uses the workspace `.venv` (1cb4e31).

**What I learned.** `create_all` can only create tables. It can't change ones that already hold data. Once the app was live, any change to the `Task` model would have silently broken things. The first deploy showed "Application failed to respond." The deploy logs showed the app was running fine on port 8080, the port Railway assigned. I had pointed the domain at 8000. The lesson: check the logs before guessing.

**What changed because of it.**
- The database setup now runs through Alembic migrations, and the test fails if the models and migrations drift apart.
- I changed the domain to port 8080, and the site came up.
- Rule added to CLAUDE.md: every model change needs a new Alembic migration.

**Working with AI.** The migration idea came from Claude reviewing my PR. I turned it into a review comment and had the agent do it as a separate PR. The CLAUDE.md rule means a later model change can't skip the migration.

**Evidence.** PR #1 (a8a67f9) and PR #2 (7909ba2, 27d78df, a9c0d5d). Commit hashes for the other pieces are listed above.

**Next.**
- Turn the roadmap into GitHub issues.
- Build the task board (Issue 2) as the first PR. It tests whether a Railway preview environment works.
- After that, build Drive search and a bare-bones coach (Issues 5–6). This tests my biggest risk early: whether the coach can find the right, current document.

## 2026-11-08 — First real searches, and what mocked tests missed

**Stage:** Building, Test

**What I did.** Over the weekend I built the core of the app: the task board (#14), Drive search (#15), Google sign-in (#16), and the first version of the coach (#17). I ran two Conductor workspaces at a time, so one agent was building while I reviewed the other's PR. The significant test was the first real Drive search for "latch." The robot account could only see this year's Mechanical folder, so it returned five docs that mention latches, but none of them said which latch we actually chose. That's exactly my problem from the proposal: the Drive has documents, not decisions. I realized the answer was sitting in a file name (`6082A12_Draw Latch.SLDPRT`) in last year's CAD folder, which the robot couldn't see. So I added the robot to the whole MuddSub shared drive as a Viewer. The file still got buried because results were sorted newest-first.

Testing the coach for real took a lot of iterations. First it said "the AI model couldn't be reached." My clipboard had a command on it instead of my key, and the app turned a bad key into a misleading "timeout" error. Once that worked, the coach used all 4 of its tool calls reading long docs and ended with "Let me check…" without ever answering, for about $0.027.

**What I learned.**
- Mocked tests can pass while the real thing is broken. It happened twice:
  - The Drive search failed for real because Google doesn't allow sorting together with full-text search, and searching a whole shared drive needs membership.
  - Google sign-in would have broken on Railway because the app sat behind Railway's proxy and built `http` callback links instead of `https`.
  - Both times, the tests passed and the real call failed. Now I always run one real call before merging.
- Smaller workflow lessons:
  - GitHub issues and PRs share one numbering, so "issue #2" in my prompts was wrong (it was #5).
  - My secret-blocking rule also blocked `.env.example`.
  - I added Railway variables to production, so the PR preview didn't have them.

**What changed because of it.**
- Drive results now rank file-name matches first, show each result's folder path and year, and group duplicates.
- The coach now requires sign-in instead of a typed name. It forces a final answer when it runs out of tool calls, caps how much of a doc it reads, treats file names as evidence, and logs tokens and cost for every call.
- With those fixes, the same question gave the right answer, McMaster 6082A12 with a link and the folder path, in 3 calls for $0.0098, about 64% cheaper.

**Working with AI.** CLAUDE.md has its first "learned the hard way" line, about trusting Railway's proxy headers. I also narrowed the secret-file rule so it stops blocking `.env.example`.

**Evidence.** PRs #14, #15, #16, #17. Commits 582d698 (fix shared Drive search scope and sorting), a764fae (improve Drive search ranking), 15a2688 (trust Railway proxy headers), 93d8a04 (list coach env vars in `.env.example`, narrow the `.env` read rule).

**Next.** The coach still says "we use" based on a file name, but a CAD file shows what's in the design, not what the team confirmed. That's why the decision record (#11) is next. It tests whether confirmed decisions can outrank Drive search results. I'm also going to build a `/ship` skill so the pre-PR checks I kept doing by hand happen automatically. Those checks are the right issue number, up to date with main, and a real call for anything touching Drive, sign-in, or the model.
