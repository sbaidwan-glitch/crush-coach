# Crush Coach workflows

Two ideas, kept apart everywhere in the app:

- **Looking at work**: a lead looks at someone's drawing/router/part, usually in person. About a task. The app never gates it: members move their own tasks through every stage, and may *notify* a lead if they want one to look. Nothing waits on a lead.
- **Recording an answer**: "this is how it is from now on." About the robot. Kept, and written to the Crush document.

Words: Suggested / Recorded / Replaced (not proposed / confirmed / superseded). The page is called **Record**.

## Who does what

| | Member | Lead (any lead) |
|---|---|---|
| Task | Start → Move to next stage (Finish task on the last), on every stage including a review stage; optionally **Notify team lead for review** (Notebook) | Moves any task too; sees notifications in the Inbox: Seen / Send back (with a reason) |
| Answers | "Ask a lead to settle this" from a coach answer | Record answer (pick the coach's suggestion, another value, or type one) |
| Tasks | Suggest a task | Add / Edit / Dismiss |
| Notebook | Draft → tidy / suggest specs → publish (no lead approval); notify a lead (separate button) | Read published entries in the Record and the weekly digest |

## Needs you (the lead Inbox tab, `docs/concepts/home-lead.html`)

One list of everything waiting on a lead, on its own **Inbox** tab (leads only); the Inbox link shows the count. Three card kinds, each with its own mark:

1. **Work to check** (square): a member notified a lead. Shows the member, the task, the stage they were on, their message, how long ago, a link to the task and to the linked entry (a published one; a draft is named, not linked, because drafts are private). **Seen** removes the card. **Send back** needs a one-line reason: if the task is still on or past the review stage it goes back to the working stage before it, and the member sees "Sent back: <reason>" in the Active task panel on Home; if it never reached the review stage it stays and only the reason is recorded. A task that merely sits on a review stage has no card. Follow-up messages add to the same card.
2. **Answer to settle** (diamond): the question, who asked and on which task, the options with their sources, the coach's suggestion preselected and labelled, or a text box when nothing answers it. Record answer / Not now.
3. **New task** (circle): suggested task with subsystem, type, what it unblocks and "done when". Add task / Edit / Dismiss.

Nothing a lead must act on lives anywhere else. Empty state: "Nothing is waiting on you."

## Notebook (`docs/concepts/task-log.html`)

A member's own entries about what they did on a task, and why. A side panel (top bar **Notebook**, the "Write today's entry" nudge when you are about to leave a page, or the link in the confirmation after "Move to next stage") with three tabs, plus **Open full page** (`/notebook`: compose on the left, history on the right, stacked on a phone).

**States.** Every entry shows a mark and the word, never colour alone:
- **Draft** (open circle): only its member can see it, and can edit it. Not in the Record, the task's History or the Crush document.
- **Published** (filled circle): read-only. To fix one, add **a follow-up**: a new entry that refers to it ("Adds to your 12 Nov entry"). Existing notes are Published.
- **Failed to publish** (filled square): published here, but the Crush document wouldn't take it. **Retry**.

**Today's entry**: task picker (defaults to the task you worked on today), photos (up to 6; the first button opens the camera on a phone; each is shrunk in the browser to about 1,600 px and 1.5 MB, the server checks the real file type and strips location data), **What I did**, **Why (recommended)**, **Tidy my note** (one model call, on the button; may add specifics only from your text, your coach chat on the task or a linked source; asks at most one question), **Suggest specs** (one model run, on the button, with the reference-book search and the calculator; each suggestion has the spec, a unit and a source link; one with no source is dropped and the page says how many; you tick which to keep and may edit the wording; accepted / edited / rejected counts are kept in `note_spec` and shown to leads on `/admin`), then **Save draft**, **Publish to Crush doc** or **Notify team lead for review** (see below).

**Publish** uploads the photos to a "Notebook photos" folder inside the writable Drive folder, then appends to the Crush document under the subsystem's tab and that day's heading: "Name · CODE: what. Why: …", a line per accepted spec, and a link to each photo. Append-only, once (`DriveWrite`). If a photo can't be uploaded the text is still published and the page says the photos stayed in the app. No lead approves an entry.

**Notify team lead for review** (blue, next to Publish; whenever the entry has a task). It records a notification for the task (a lead sees it in the Inbox) and nothing else: it does not publish, saves nothing, and moves nothing; publishing does not notify. Nothing is added to the side panel: a pop-up appears in the middle of the screen over a blurred page, "Your team lead has been notified. Do you want to add a message for them?", with a text box ("e.g. Please confirm the hole spacing", 280 characters), **Send message**, an X, and a thin bar at the bottom that closes it by itself after 10 seconds (it waits while you type). While one is unanswered, pressing again adds nothing and the pop-up says when it was sent and lets you add or change the message; at most 3 new notifications per task per day. It links your saved draft (or the entry you follow up, or today's published entry for the task) when there is one, else only the task.

**Earlier**: your entries, 20 at a time, filter by task, each with its state and (if published) **Open in doc**.

**Private notes**: words only you can read. No task, no Drive, no model, no publish button; no other member or lead can see them anywhere.

Drafts and their photos are served only to their member; published photos to any signed-in member. Entries also appear in the task's History and the Record (as a Note, behind "Show work notes").

## Plan my next steps

A button under the latest coach answer. One streamed model call over the same retrieval, no tools, about 250 tokens, in a fixed shape: **What I found** (with sources) · **What's missing** · **Do this now** · **If you need more help** (a ready-to-paste search prompt in a box with a Copy button and the warning "This text includes information from the team Drive. Paste it only into tools your team allows.") · **You should end up with** (outputs, described, no numbers) · **Then** (the task's next step, from `steps.py`, no model). It uses one of your daily coach messages. Home's "Next step" shows its "Do this now".

## Record (`docs/concepts/record.html`)

Read-only. By subsystem, grouped by date, newest first. **Answer** entries (bold statement, why, what it replaces, who recorded it) and **Note** entries (toggle "Show work notes"). Link to the same place in the Crush document. Search.

## Rules

- Any lead can settle an answer or answer a notification (Seen / Send back). A lead who notifies themselves gets a card like any other. Old History rows that say "approved" or "self-approved" stay as they were.
- Answers need a lead. Notebook entries and task moves don't.
- Drive writes are append-only, signed and dated; a failed write keeps the entry in the app as "Failed to publish" with Retry.
- The model runs only on a button press: Ask, Recommend, Plan my next steps, Tidy my note, Suggest specs, Explain this, Weekly review.
