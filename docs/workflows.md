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
| Notebook | Draft → tidy / suggest specs → publish (no lead approval) | Read published entries in the Record and the weekly digest |

## Needs you (the lead Inbox tab, `docs/concepts/home-lead.html`)

One list of everything waiting on a lead, on its own **Inbox** tab (leads only); the Inbox link shows the count. Three card kinds, each with its own mark:

1. **Work to check** (square): task, who, which step, how long it's waited, links. Accept / Send back. On Accept, one optional line "Anything decided here worth recording?" saves a recorded answer for that task's subsystem.
2. **Answer to settle** (diamond): the question, who asked and on which task, the options with their sources, the coach's suggestion preselected and labelled, or a text box when nothing answers it. Record answer / Not now.
3. **New task** (circle): suggested task with subsystem, type, what it unblocks and "done when". Add task / Edit / Dismiss.

Nothing a lead must act on lives anywhere else. Empty state: "Nothing is waiting on you."

## Notebook (`docs/concepts/task-log.html`)

A member's own entries about what they did on a task, and why. A side panel (top bar **Notebook**, the Home banner "Write today's entry", or after "Mark step done") with three tabs, plus **Open full page** (`/notebook`: compose on the left, history on the right, stacked on a phone).

**States.** Every entry shows a mark and the word, never colour alone:
- **Draft** (open circle): only its member can see it, and can edit it. Not in the Record, the task's History or the Crush document.
- **Published** (filled circle): read-only. To fix one, add **a follow-up**: a new entry that refers to it ("Adds to your 12 Nov entry"). Existing notes are Published.
- **Failed to publish** (filled square): published here, but the Crush document wouldn't take it. **Retry**.

**Today's entry**: task picker (defaults to the task you worked on today), photos (up to 6; the first button opens the camera on a phone; each is shrunk in the browser to about 1,600 px and 1.5 MB, the server checks the real file type and strips location data), **What I did**, **Why (recommended)**, **Tidy my note** (one model call, on the button; may add specifics only from your text, your coach chat on the task or a linked source; asks at most one question), **Suggest specs** (one model run, on the button, with the reference-book search and the calculator; each suggestion has the spec, a unit and a source link; one with no source is dropped and the page says how many; you tick which to keep and may edit the wording; accepted / edited / rejected counts are kept in `note_spec` and shown to leads on `/admin`), then **Save draft** or **Publish to Crush doc**.

**Publish** uploads the photos to a "Notebook photos" folder inside the writable Drive folder, then appends to the Crush document under the subsystem's tab and that day's heading: "Name · CODE: what. Why: …", a line per accepted spec, and a link to each photo. Append-only, once (`DriveWrite`). If a photo can't be uploaded the text is still published and the page says the photos stayed in the app. No lead approves an entry.

**Earlier**: your entries, 20 at a time, filter by task, each with its state and (if published) **Open in doc**.

**Private notes**: words only you can read. No task, no Drive, no model, no publish button; no other member or lead can see them anywhere.

Drafts and their photos are served only to their member; published photos to any signed-in member. Entries also appear in the task's History and the Record (as a Note, behind "Show work notes").

## Plan my next steps

A button under the latest coach answer. One streamed model call over the same retrieval, no tools, about 250 tokens, in a fixed shape: **What I found** (with sources) · **What's missing** · **Do this now** · **If you need more help** (a ready-to-paste search prompt in a box with a Copy button and the warning "This text includes information from the team Drive. Paste it only into tools your team allows.") · **You should end up with** (outputs, described, no numbers) · **Then** (the task's next step, from `steps.py`, no model). It uses one of your daily coach messages. Home's "Next step" shows its "Do this now".

## Record (`docs/concepts/record.html`)

Read-only. By subsystem, grouped by date, newest first. **Answer** entries (bold statement, why, what it replaces, who recorded it) and **Note** entries (toggle "Show work notes"). Link to the same place in the Crush document. Search.

## Rules

- Any lead can settle an answer or check work. A lead can accept their own work: the card says so, and the task's History records it as "self-approved".
- Answers need a lead. Notebook entries don't.
- Drive writes are append-only, signed and dated; a failed write keeps the entry in the app as "Failed to publish" with Retry.
- The model runs only on a button press: Ask, Recommend, Plan my next steps, Tidy my note, Suggest specs, Explain this, Weekly review.
