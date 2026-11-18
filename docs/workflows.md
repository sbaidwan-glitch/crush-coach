# Crush Coach workflows

Two ideas, kept apart everywhere in the app:

- **Checking work**: "this person's drawing/router/part is good, move on." About a task. Done once.
- **Recording an answer**: "this is how it is from now on." About the robot. Kept, and written to the Crush document.

Words: Suggested / Recorded / Replaced (not proposed / confirmed / superseded). The page is called **Record**.

## Who does what

| | Member | Lead (any lead) |
|---|---|---|
| Task | Start → Mark step done → Ask for review (one main button at a time) | Accept / Send back |
| Answers | "Ask a lead to settle this" from a coach answer | Record answer (pick the coach's suggestion, another value, or type one) |
| Tasks | Suggest a task | Add / Edit / Dismiss |
| Work notes | Log today's work → tidy → post (no lead approval) | Read them in the Record and the weekly digest |

## Needs you (the lead Inbox tab, `docs/concepts/home-lead.html`)

One list of everything waiting on a lead, on its own **Inbox** tab (leads only); the Inbox link shows the count. Three card kinds, each with its own mark:

1. **Work to check** (square): task, who, which step, how long it's waited, links. Accept / Send back. On Accept, one optional line "Anything decided here worth recording?" saves a recorded answer for that task's subsystem.
2. **Answer to settle** (diamond): the question, who asked and on which task, the options with their sources, the coach's suggestion preselected and labelled, or a text box when nothing answers it. Record answer / Not now.
3. **New task** (circle): suggested task with subsystem, type, what it unblocks and "done when". Add task / Edit / Dismiss.

Nothing a lead must act on lives anywhere else. Empty state: "Nothing is waiting on you."

## Work log (`docs/concepts/task-log.html`)

- Open from "Log today's work" (top bar, every page), from the Home banner ("You worked on M06 today…", shown when the member was active today (moved a task or asked its coach) and hasn't logged that task; "Not today" hides it until tomorrow), or after "Mark step done".
- A side panel with two tabs: **Log today's work** and **My notebook**.
- Two boxes: "What did you do?" and "Why did you do it that way? (recommended)".
- **Tidy my note** (one model call, on the button): rewrites for clarity. It may add specifics only from the member's text, their coach chat on that task, or a linked source. Never invents a number, part or name. If a detail is missing it asks one question in a box ("which two corners?") instead of guessing.
- The member sees the tidied note in the two boxes, can edit, then **Post to the Crush document**. Nothing is saved before that. Tidy can be skipped ("Post as written"). The member's own words are kept with the note.
- It is appended to the Crush document: subsystem tab (or "General", and it says so) → that day's date heading (created if missing) → "Name · CODE: what. Why: …". Append-only, once (`DriveWrite`). If Google fails the note stays in the notebook with **Retry write to Drive**.
- It also appears in the member's notebook, the task's History and the Record (as a Note, behind "Show work notes").
- Recorded answers are written to the same document when a lead records them: bold statement, why, what it replaces, sources, who recorded it. Set-up and behaviour: `docs/crush-document.md`.

## Record (`docs/concepts/record.html`)

Read-only. By subsystem, grouped by date, newest first. **Answer** entries (bold statement, why, what it replaces, who recorded it) and **Note** entries (toggle "Show work notes"). Link to the same place in the Crush document. Search.

## Rules

- Any lead can settle an answer or check work. A lead can accept their own work: the card says so, and the task's History records it as "self-approved".
- Answers need a lead. Work notes don't.
- Drive writes are append-only, signed and dated; a failed write keeps the entry in the app with "Retry write to Drive".
- The model runs only on a button press: Ask, Tidy my note, Explain this, Weekly review.
