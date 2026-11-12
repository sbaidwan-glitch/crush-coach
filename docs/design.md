# Crush Coach design: "Drafting"

The look of an engineering drawing: dot-grid paper, square panels, ruled lines, mono task codes, and Crush itself on a blueprint grid. Calm and readable first; the theme is in the details, not the decoration.

**Source of truth:** `docs/concepts/*.html` (open them in a browser) plus `src/static/styles/tokens.css` and `src/static/styles/app.css`. Templates should reuse those class names. Reference screenshots sit next to each concept (`*-1440.png`, `*-390.png`).

## Pages

| Concept | Route | Who it's for | What it answers |
|---|---|---|---|
| `home.html` | `/` | every member | What should I do next? |
| `tasks.html` | `/tasks` | everyone, mostly leads | Where is the team? |
| `task.html` | `/tasks/{code}` | the task's owner | How do I do this task, and what's its state? |

Decisions, admin and sign-in pages reuse the same shell, panels and fields; no new components.

**Home**: Your task (the signed-in member's in-progress task: stage strip, the coach's latest "Next step" for it, Open task / Ready for review), Pick up a task (up to 4 unclaimed tasks with Claim), Build progress (total, one bar segmented by subsystem, a swatch per subsystem linking to `/tasks#subsystem`), Crush (the render with a tag per subsystem showing done/total; each tag links to that subsystem on `/tasks`).
Empty states: no task yet → "You haven't started a task. Pick one on the right." No unclaimed tasks → "Every task has an owner."

**Tasks**: filters All / Mine / Unclaimed / Needs review; counts box (Done x/total, In progress, Review, Unclaimed); one flush panel per subsystem with a ruler progress bar (one tick per task) and task rows; right column: Recent decisions (3 newest, proposed ones marked "needs a lead") and, for leads only, Waiting for review.

**Task**: left: Coach thread; right: Progress (stage strip, owner, status buttons), About this task (tracker description, related decisions), History (TaskEvent list).

## Rules

1. **Square corners.** No `border-radius` except status dots. 1px `--color-rule` borders, no shadows (only the Crush render has a drop shadow).
2. **Panels** have a 3px top rule: accent blue for content panels, ink for "reference" panels (Crush, Recent decisions, About this task).
3. **Type:** Manrope for everything; JetBrains Mono only for task codes, decision ids and CAD tags. Sizes come from tokens: 36 page title, 21 panel title, 15 body, 13 meta, 12 status. Uppercase only for small field labels and the counts box.
4. **Colour:** ink, muted and blue do almost all the work. Status colours appear only on status marks and counts. Subsystem colours appear only on progress bars and subsystem diamonds.
5. **Status is never colour alone.** To do = open circle, In progress = half circle, Needs review = filled square, Done = filled circle. Always with a text label (except inside a render tag, where the tag text says done/total).
6. **Coach messages:** the member's message is a filled box offset right; the coach's has a blue left rule. Sources are a ruled list of links with type and date. "Next step:" sits in a blueprint-blue box at the end. Conflicts sit in a review-coloured box that leads with **Likely current: …**, then the older value and its source, and ends with "a lead should confirm" plus a "Propose as a decision" button. Errors keep the member's question, say what failed in plain words, and offer Retry. While waiting, show the scanning bar with what it's doing ("Searching the Drive index…"); respect reduced motion.
7. **One progress bar everywhere** (`.pbar`): a ruler. Tick marks above, one space per task; a bar below in the subsystem's colour (`--color-sub-1…5`, same as its diamond swatch): solid = done, mid tint = in progress, light tint = not started. Home shows every subsystem's ruler side by side; each subsystem panel on Tasks shows its own. Never draw progress any other way.
8. **Buttons:** one primary (filled ink) per panel at most. Secondary = outlined. Small buttons inside rows.
9. **Navigation:** left sidebar with icon + label (Home, Tasks, Decisions; Admin under "LEAD", leads only). Under 820px it becomes a bottom tab bar.
10. **Accessibility:** every icon link has a visible label; `aria-current` marks the current page and the current stage; focus ring from `--focus-ring`; muted text passes 4.5:1 on panels. Check every page at 375px and 1280px.
11. **The model is never called on page load.** "Next step" on Home is the stored line from the last coach answer, not a fresh call.

## Assets

- Fonts: `src/static/fonts/manrope/` and `src/static/fonts/jetbrains-mono/` (woff2, latin, with `OFL.txt` licences).
- `src/static/img/crush.png`: CAD render of Crush with a transparent background. Tag positions on Home are percentages of the image (Enclosure 54%/31%, Seals 66%/50%, Thrusters 12%/38%, Actuators 69%/66%); keep them in one config dict so they're easy to move if the render changes.
