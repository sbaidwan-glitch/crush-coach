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

**Evidence:** The concept screens the people I talked to were shown: [Quest Board](process/2026-11-03-concept-quest-board.png) and [Build the Sub](process/2026-11-03-concept-build-the-sub.png).

## 6 Nov 2026: Repo setup
**What I did:** Created the repo from the course template, removed lab-only files, and set up CLAUDE.md, permission rules that block the agent from reading secret files, and this log. Stored the Glassroom key in macOS Keychain and loaded it into the shell environment, so it is never in a file. Chose to work locally with Conductor instead of Codespaces.

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

## 2026-11-09 — Prototype presentation: the value is organizing the legacy mess

**Stage:** Test, Define

### What I did

I presented the problem, the interviews and the prototypes to my class. I asked what progress signal would make them open a club tracker weekly, and where they'd look for a saved decision a year later.

**What I learned.**
- The most valuable thing isn't answering one question. It's organizing the legacy mess: what was decided, why, what we rejected ("why didn't we go down this route?"), and which docs are current vs. stale.
- When docs disagree, people don't want the AI to just list both. They want it to suggest the most likely answer (or say "either could work, here's what to consider") and let a lead confirm.
- Someone asked why I use the Drive API instead of a Drive MCP server. An MCP server lets an AI client like Claude Desktop browse the Drive. My app needs the Drive inside my own database so search is fast and repeatable, so I use the API directly. Wrapping my index as an MCP server is a possible extension.
- After the presentations, what I liked was the idea of a dashboard with Crush in the center and panels around it, more like a mission console than a scrollable website.

**What changed because of it.** Organizing legacy knowledge is now the priority: decision records with reasoning and rejected alternatives, and discrepancies flagged with a recommended answer for a lead to confirm. I added a dashboard with a 2D Crush view to the plan. 3D CAD stays a stretch goal.

**Evidence.** The decision records landed in #28. The Drive/MCP and dashboard decisions are plan changes with no commits yet.

**Next.** Flag discrepancies with a recommended answer, and build the dashboard. This tests whether people prefer a suggested answer to a list of both.

## 2026-11-11 — The coach was a slow chatbot, so I changed how it works

**Stage:** Test, Building

### What I did

I tested the coach on real tasks (M02, M03.5, E01). It took 20–50 seconds, asked me "which season?", timed out, and when it did answer it just pointed at one file instead of putting things together. Instead of patching prompts again, I stepped back and changed the architecture: read the whole Drive once into our own database, then answer from that. Before rebuilding anything, I built `make eval`: 15 real questions members ask, each with the facts a good answer must contain, the source it should cite, and things it must not say.

**What I learned.**
- Baseline on the old coach: **pass 3 / partial 2 / fail 11, median 21.5 s, max 57 s, $0.30 for 16 questions**. That's about $0.02 each, so the $1/5 h cap is only about 50 questions for the whole team.
- When I read the actual answers, 5 of the 11 fails weren't the coach. 4 were provider errors from firing questions back to back, and 1 was my test wording ("not decided" vs. "isn't recorded"). The test needed testing too.
- The real failures had one root cause. The coach searched Drive live, one tool call at a time, and couldn't open spreadsheets, so values like the gasket clearance were out of reach.
- Indexing the Drive took about 1.5 hours for 47,831 files, with zero read errors. Searches now come back in under a second. "Gasket clearance" found the Seal Calculations sheet, the dated 0.050" change in Crush Documentation, and the tracker note that the clearance is still unresolved. The old coach never reached any of these.
- The index still missed the 6082A12 latch drawing (CAD/PDF files have no text, so wordy docs outrank them). It ranked a "Copy of" doc as a separate hit. It also read one spreadsheet tab with the wrong header row, which would have given wrong owners. Fast search isn't trustworthy search until I check what it returns.

**What changed because of it.**
- The coach will do one model call over results from our own index instead of many live Drive calls, streamed so text appears right away.
- I'm adding team facts (current season, current vehicle) so it stops asking basic questions.
- I set targets before rebuilding so I can't move the goalposts: **at least 11/15 pass, median under 6 s, under $0.01 per question.**
- I dropped /ship. It added time without catching anything that tests and the eval don't.

**Working with AI.** `make eval` is now the rule for any coach change: run it before and after and paste both summary lines in the PR (in CLAUDE.md). I dropped the /ship skill I wrote on 8 Nov.

**Evidence.** `make eval` in #27 (7d29dfa questions, 2544342 scoring, 15d8e73 runner, 51350d1 question cleanup). Earlier coach fixes in #26. The indexing run and baseline numbers aren't in git yet.

**Next.** Rebuild the coach on the index and run `make eval` against the three targets. This tests whether answering from our own index fixes the root cause, or whether retrieval quality (CAD/PDF files, duplicates, spreadsheet headers) is the real limit.

## 2026-11-23 — Design review

**Stage:** Test

**What I did.** On Monday 23 Nov, during the class design review, three classmates in my group of four each tried the app on their own device with no help from me and filled in a worksheet. 

**What I learned.**
- All three finished the main task and said it was smooth. None found it slow or broken.
- The home display repeats itself. Build progress and the Crush labels do the same job, and both feel secondary to tasks. One reviewer suggested quadrants with the important things on top.
- All tasks is a little overwhelming. One reviewer suggested shades to show what's mine at a glance.
- Record vs. notebook was confusing at first. Only later did they realize the record is settled across the team and the notebook is personal.
- After suggesting something, it doesn't show up as pending.
- Having a lead review on every task might slow people down. One reviewer asked who owns approvals and how much management I'd have to do.
- What would make them use it: email notifications, a weekly summary, and seeing what others are working on.
- Nobody tested whether the coach's answers are right.

**What surprised me.** Everyone found the dashboard pretty intuitive. I thought I'd have more questions about the overall feel.

**What changed because of it.** Nothing is built yet. Because of the review I will:
- clean up the task page for a better user workflow
- create a better way of recording answers for the team vs. what's personal
- make sure the team knows which tasks are pending review
- change the look of the panels
- implement reminders

I have no plan yet for the approvals question or for checking the coach's answers. "Seeing what others are working on" is what the Timeline (#43) is for, but I built that on 17 Nov, before the review, so it isn't a response to it. 

**Evidence.** The three filled-in worksheets: [worksheet 1](process/design-review-worksheet-1.png), [worksheet 2](process/design-review-worksheet-2.png), [worksheet 3](process/design-review-worksheet-3.png).

**Next.** Build the five changes above.

## 2026-11-26 — 12–26 Nov: A working team tool, a new Home, and a cheaper coach

**Stage:** Building, Prototype

**What I did.** Between my 11 Nov entry and today I turned the prototype into something a team could use. The reasons below come from the PR descriptions.

*The look.* On 11 Nov Home was a heading and one button on a dark page. I rebuilt it in the Drafting design: a sidebar and four panels (Your task, Pick up a task, Build progress, Crush) (#31). On 17 Nov I added a tint per panel so no two Home panels share a colour, plus a Light/Dark toggle and a Bubbles toggle (#42). I removed the "Recently recorded" panel from All tasks because the Record page already shows those answers (#37).

*Task flow.* Tasks now move through steps that fit their type (Fabricate, Print, Design, Validate, Investigate) instead of one list for all. I dropped the "AI pre-check" step because that feature doesn't exist (#35). A lead can change a task's type and edit its steps. I added the Timeline, with one bar per task from the day it was picked up to the day it was finished, so there is more awareness of what other members are working on. It is also just better task management. Members can now pick an icon too (#43).

*Leads.* I replaced the separate review panels and the propose/accept/supersede screens with one Inbox for leads and a Record of settled answers (#36), to simplify lead review and decisions. Leads can also remove tasks and update existing ones on re-import (#32). Signed-out visitors get a read-only preview with blurred, made-up task text, so recruiters can see progress without real names in the page (#34).

*Work log.* Members can log today's work. A "Tidy my note" button cleans up the note without inventing numbers, parts or names. Posted notes are appended to the Crush document, never edited (#38). This goes back to my 28 Oct problem: decisions and status were buried in notebooks and CAD.

*The coach.* The rebuild I planned on 11 Nov landed in #33. It answers from my own Drive index in one streamed call, with team facts. I couldn't run `make eval` for it because that workspace had no API key. #39 then went after the cost, which was about $0.044 per question, and the cut-off and empty answers. The results on 17 questions:

| Model | Result | Speed | Cost per question |
|---|---|---|---|
| Sonnet | pass 12 / partial 3 / fail 2 | median 6.9 s | $0.0361 |
| Haiku | pass 10 / partial 4 / fail 3 | median 3.7 s | $0.0074 |
| Gemini Flash-Lite | pass 10 / partial 4 / fail 3 | median 1.6 s | $0.0023 |

Offline, questions whose prompt held everything needed went from 8/15 to 13/15. Sonnet misses my 11 Nov targets for speed (under 6 s) and cost (under $0.01), so I kept it as the default for now. The coach could only repeat the Drive: asked "what would an adapter look like?", it said "No detailed design exists in the sources". So #40 added a Recommend button that searches the reference books and does the arithmetic with a calculator tool. A lead still decides.

**What changed because of the review.** Nothing in this window. See the 23 Nov entry.

**Evidence.** PRs #31–#40, #42 and #43.
- Home before: [11 Nov](process/2026-11-11-app-home-before-redesign.png)
- Drafting redesign: [15 Nov](process/2026-11-15-app-home-drafting-redesign.png)
- Tinted panels: [26 Nov](process/2026-11-26-app-home-tinted-panels.png)

**Next.** Do the changes I planned after the design review (see the 23 Nov entry). Also re-run `make eval` on the final commit, which the test plan in #39 left unchecked.
