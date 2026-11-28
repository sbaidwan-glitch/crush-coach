# Crush Coach design: "Polar"

Navy instrument panels on a pale page, one orange accent, rounded 4px corners. Mono for codes, counts and labels; Manrope for everything you read. Calm and readable first; the motion is slow, decorative and always optional.

**Source of truth:** `docs/concepts/polar.html` and `polar.css` (open the page in a browser; its animations work), with the screenshots `polar-light-1440.png`, `polar-dark-1440.png`, `polar-light-390.png`, `polar-dark-390.png`. The app's tokens are `src/static/styles/tokens.css` (light) and `theme.css` (dark, the shell, Home). The older concept pages in `docs/concepts/` (`home.html`, `tasks.html`, ...) are the Drafting design this replaced and are kept only for their page structure.

## Pages

| Route | Who it's for | What it answers |
|---|---|---|
| `/` (Home) | every member | What should I do next? |
| `/tasks` | everyone, mostly leads | Where is the team? |
| `/tasks/{code}` | the task's owner | How do I do this task, and what's its state? |
| `/inbox` | leads | What is waiting on me? |
| `/timeline`, `/record`, `/notebook`, `/profile`, `/admin` | | |

Only Home is laid out as the concept. Every other page keeps its own layout and takes the new look through the shared tokens and components (`.panel`, `.btn`, `.status`, rows, fields).

**Home** (`/`): header (eyebrow "Crush / Build phase", title "Build control", lede, **+ Log today's work**), then three tiles (**Notes this week**: published notes only, plus "N drafts of yours" when the member has drafts; **Your tasks in review**; **Available tasks**), then the grid: a tall **Vehicle overview** on the left (Crush on the blueprint grid with a tag per part from `progress.SUBSYSTEM_GROUPS`, the gauge, a bar per subsystem, Open build map), a wide **Active task** (stage strip, the stored "Next step" line, Open task / Ask for review or Mark step done / Ask the coach) and a full-width **Available tasks** panel with Claim buttons, and that's the page. Home has no Notebook banner: its only Notebook links are the header's **+ Log today's work** and the sidebar's Notebook (the "You worked on M06 today" nudge lives on every page, see rule 13). Under 1100px it is one column. Signed-out visitors see the same page as a preview: real progress, only the Available tile, blurred task titles, Claim disabled.
Empty states: no active task → "You haven't started a task. Pick one from Available tasks below." No unclaimed tasks → "Every task has an owner."

**Tasks**: filters All / Mine / Unclaimed / Needs review; counts box; one flush panel per subsystem with a ruler progress bar (one tick per task) and task rows; Suggest a task. **Notebook** (`/notebook`, and the side panel): Today's entry, Earlier, Private notes. **Task**: Coach thread on the left; Progress, Type and steps (leads), About this task, History on the right.

## Rules

1. **Corners are 4px** (`--radius`) on panels, buttons, tags, fields and boxes. Never square, never a pill. The only things not rounded by this are the status marks' own shapes (square for "needs review", diamond for a subsystem). 1px borders, no shadows (only the Crush render has a drop shadow).
2. **Panels.** Two kinds. A **navy instrument panel** (`.panel--navy`; Home's Vehicle overview and Active task) re-points the page's colour tokens to the panel set (`--panel`, `--panel-ink`, `--panel-muted`, `--panel-rule`, `--panel-inset`), so rows, status marks, fields and buttons inside it need no rules of their own. Every other panel is **paper** (`.panel`: `--paper` with a `--paper-line` border). Panels have **no coloured top rule and no tint**. The only top rules in the app: the orange rule on Home's **Available tasks** panel, and the navy (orange on the last) top rule on the **tiles**; they belong to those components. Do not add `data-tone`.
3. **Type:** Manrope for text; JetBrains Mono for task codes, ids, counts, the nav, eyebrows, pills and meta lines. No other fonts. Sizes come from tokens: 40 page title (32 on phones), 21 panel title, 15 body, 13 meta, 12 status. Uppercase mono only for small labels.
4. **Colour:** one accent, orange (`--orange`): the active nav item, primary buttons, the current step, bars, the gauge. **Text on orange is dark navy (`--on-orange`)**: white on orange fails contrast. Orange as *text* is `--orange-text-on-page` or `--orange-text-on-panel` (darker on pale, lighter on navy). Links and the "needs review" mark use it; in progress is blue (`--info`); done is green (`--ok`); errors are `--danger`. Subsystem colours (`--color-sub-1…5`) appear only on progress rulers, diamonds and Timeline bars; Home's bars are orange.
5. **Status is never colour alone.** To do = open circle, In progress = half circle, Needs review = filled square, Done = filled circle. Always with a text label (except inside a render tag, where the tag text says done/total).
6. **Coach messages:** the member's message is a filled box offset right; the coach's has an orange left rule. Sources are a ruled list of links with type and date. "Next step:" sits in a tile-coloured box at the end. Conflicts sit in a review-coloured box that leads with **Likely current: …**. Errors keep the member's question, say what failed in plain words and offer Retry. While waiting, show the scanning bar with what it's doing; respect reduced motion.
7. **Progress** is drawn two ways, nowhere else: the **ruler** (`.pbar`: ticks above, one space per task; a bar below in the subsystem's colour, solid = done, mid tint = in progress, light tint = not started) on Tasks, Timeline and wherever a part is shown; and Home's **gauge and bars** (an SVG ring and a 4px bar per subsystem), both computed from the real numbers in `progress.py`.
8. **Buttons:** one primary (orange fill, navy text) per panel at most. Secondary = outlined (`.btn`); on a navy panel it is the `--panel-btn` fill. Small buttons inside rows.
9. **Navigation:** a navy sidebar: the CRUSH wordmark and "Underwater robotics", a numbered mono nav (01 Home, 02 Inbox with the Needs-you count for leads, 03 Tasks with the open count, 04 Timeline, 05 Notebook, 06 Team record; numbered in the order shown, so a member, who has no Inbox, sees Tasks as 02), the orange active item (`aria-current="page"`), the sonar graphic, the workspace name, and a bottom row with **Lead / Admin** (a link to `/admin`, leads only) and the **Light/Dark** button. Under 820px the sidebar is a bottom tab bar of labels only (no numbers or counts; it scrolls sideways if it has to; Lead / Admin and Light/Dark are tabs at its end). The "+ Log today's work" / Notebook button and the member's icon chip stay in the page header. Dark mode follows the system until someone presses Light/Dark; the choice is kept in `localStorage` as `cc-theme` and a script in `<head>` applies it before first paint. Dark overrides tokens only.
10. **Accessibility:** every control has a visible label; `aria-current` marks the current page and the current step; **keyboard focus is a 2px orange outline** (`:focus-visible`, 2px offset) on every control; muted text passes 4.5:1 on every surface it sits on. Check every page at 375px and 1280px, in both themes.
11. **The model is never called on page load.** "Next step" on Home is the stored line from the last coach answer, not a fresh call.
12. **Animation** is CSS only, slow and decorative: the sonar sweep, the drifting wave lines, Crush's slow bob, the gauge's tick ring turning, the gauge arc and bars filling in, and each Home block fading up once. Decorations are `aria-hidden`. **Every animation is off under `prefers-reduced-motion` and in print** (`theme.css` ends with both rules), and the page must be complete without them: the gauge and bars are drawn at their final state and their animation only runs *from* empty, so turning it off leaves them full. Nothing animates on click, and nothing moves faster than a second except the one-time fills.

13. **The Notebook nudge** ("You worked on CODE today. Write today's entry while you remember it.", with **Write today's entry** and **Not today**) is not part of any page's layout. `base.html` renders it hidden on every signed-in page when `notebook.banner` says there is something to write up, and `notebook.js` shows it **only on exit intent**: the pointer leaving the top of the window (no button pressed), or the tab becoming hidden and then returning. It is a small dismissible corner prompt (`role="status"`, no backdrop, never takes focus, closes on Esc and on Not today, not printed), shown at most once per local day per member, never while the Notebook drawer is open, and never again after Not today or after the Notebook was opened. No timers, no polling. After "Mark step done" the drawer still opens by itself (`?log=CODE`).

## Accessibility: contrast

WCAG 2.1 AA, measured from the tokens. `tests/test_design_tokens.py` (`CONTRAST_PAIRS`) fails if any pair drops below 4.5:1 in light or dark, so this table cannot drift. The lowest pair is 5.1:1.

| Text on background | Where | Light | Dark |
|---|---|---|---|
| `ink` on `page` | Body text on the page | 13.6 | 16.6 |
| `muted` on `page` | Muted text on the page | 6.2 | 8.8 |
| `orange-text-on-page` on `page` | Eyebrow, links on the page | 5.1 | 9.2 |
| `ink` on `paper` | Text on a paper panel | 15.4 | 15.2 |
| `muted` on `paper` | Muted text on paper | 7.0 | 8.0 |
| `orange-text-on-page` on `paper` | Links, review mark | 5.7 | 8.4 |
| `info` on `paper` | In-progress mark | 7.1 | 8.6 |
| `ok` on `paper` | Done mark | 6.2 | 8.5 |
| `danger` on `paper` | Error text | 6.4 | 8.4 |
| `ink` / `muted` / orange on `field` | Text in a field | 14.1 / 6.5 / 5.3 | 13.4 / 7.1 / 7.4 |
| `tile-ink` / `tile-muted` / `muted` on `tile` | Tiles, the nudge, Next-step box | 11.3 / 5.7 / 5.2 | 14.2 / 7.5 / 7.5 |
| `panel-ink` on `panel` | Text on a navy panel | 11.9 | 13.9 |
| `panel-muted` on `panel` | Muted text on navy | 6.9 | 7.7 |
| `orange-text-on-panel` on `panel` | Accent text on navy | 6.6 | 7.7 |
| `info-on-panel` / `ok-on-panel` / `danger-on-panel` on `panel` | Status marks, errors on navy | 6.7 / 6.7 / 6.5 | 7.9 / 7.8 / 7.7 |
| `panel-ink` / `panel-muted` / orange on `panel-inset` | Steps, pills, Next step on navy | 9.7 / 5.7 / 5.4 | 15.2 / 8.4 / 8.4 |
| `panel-ink` on `panel-btn` | Secondary button on navy | 8.4 | 11.0 |
| `on-orange` on `orange` | Primary button, active nav, current step | 6.4 | 7.1 |
| `side-ink` on `side` | Sidebar links | 8.1 | 7.7 |
| `panel-ink` on `side` | Wordmark | 11.9 | 17.2 |
| `orange-text-on-panel` on `side` | Mode button | 6.6 | 9.5 |

Fixed while building this: the page's orange link text is 4.2:1 on a tile, so links on tiles and in Next-step boxes use the tile's ink, underlined. White on orange is 2.7:1 (light) and 2.5:1 (dark) and is never used.

## Assets

- Fonts: `src/static/fonts/manrope/` and `src/static/fonts/jetbrains-mono/` (woff2, latin, with `OFL.txt` licences). Nothing else.
- `src/static/img/crush.png`: CAD render of Crush with a transparent background. Tag positions on Home are percentages of the image; they live in `progress.SUBSYSTEM_GROUPS` (one dict), so they are easy to move if the render changes.
- `src/static/img/waves.svg`: the drifting lines in the Active task panel (decorative; a CSS background).
