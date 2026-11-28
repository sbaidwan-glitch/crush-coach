# Timeline and icons

Reference: `docs/concepts/timeline.html`, `profile.html` (+ `timeline-light-1440.png`, `timeline-dark-1440.png`, `timeline-light-390.png`, `profile-light-1440.png`). Styles: `src/static/styles/timeline.css`, loaded after `theme.css`.

## Timeline (`/timeline`)
- One panel per subsystem; its bars take the subsystem's colour (the same as its progress ruler, set as `--tone` on the panel). One row per task: code, title, status mark with text, then a bar.
- **A bar runs from the day the task was picked up to the day it was finished.** Both come from `TaskEvent`, no new date fields:
  - start = the task's latest "start/claim" event (a task unclaimed and picked up again starts again);
  - end = its "done" event; an open task runs to today;
  - a task already in progress before the tracker had events starts at its first event. `Task` has no `created_at`, so a started task with no events at all has no date to draw from: it gets no bar and is named in a second foot line ("Started before the tracker kept dates") rather than given an invented date;
  - a task on a review stage ends the day it moved onto it (as in `timeline-light-1440.png`; telling a lead about a task is not a move and draws nothing); a task sent back is open again and keeps its start.
  - Not-started tasks get no bar; each panel lists them in one line at its foot.
- Bar styles (never colour alone): finished = solid; open = tinted with an outline, ending at today; on a review stage = solid with a review-coloured outline. The status text in the label says which.
- The person's icon chip sits at the bar's tip with their short name; two owners stack two chips ("Name +1"). A "Today" line crosses every row. The axis shows weeks; its range is the earliest start to the latest end (today counts as an end), plus a week either side, beginning on a Monday. One column per week, or two or three once there would be more than ten, so labels never collide (`--col` on `.tl-inner` sets the grid lines).
- Phones: the chart scrolls sideways inside its own container; the label column, each panel's title and its foot lines stay pinned, and `timeline.js` opens the chart scrolled to Today.
- Read-only for everyone; no model calls.

## Icons
- Members pick one of 9 icons (submarine, drill, octopus, claw, torpedo, wrench, anchor, gear, fish) and a short name (20 characters) at `/profile`. New table `member_profile` (email, icon, display_name, plus `full_name`: the Google name they signed in with, which is what a task's `owner` holds, so a task can find its owner's icon); until a member picks, they show their Google first name and the gear icon.
- The icons are our own line drawings in `components/icons.html` (24px, 1.8 stroke). The chip is a square with a 1px ink border, matching the rest of the design. No Google profile photos are stored or loaded.
- The chip also appears next to the owner on task rows and the task page, and the signed-in member's own chip in the top bar opens `/profile`. Two owners in an owner cell ("A, B", "A & B", "A and B") stack two chips.
- A member edits only their own profile: the form has no email or id, the row is always the signed-in person's. Icon must be one of the nine; name at most 20 characters (checked on the server).
