# Theme: tinted panels, dark mode, bubbles

Reference: `docs/concepts/home.html` and `tasks.html` (they load `src/static/styles/theme.css`); screenshots `*-light-*.png`, `*-dark-*.png`.

## 1. A muted colour per panel
- A panel gets `data-tone="blue|teal|sage|sand|clay|mauve|slate|rose"`. The tone sets the panel's background tint and its 3px top rule. Tokens live in `theme.css` (light and dark values for each).
- **On /tasks a subsystem panel uses its subsystem's tone, the same colour as its progress bar and diamond:** Enclosure = blue, Seals = teal, Thrusters = sage, Actuators = sand. Subsystem colours therefore keep one meaning everywhere.
- Panels that are not a subsystem never use those four: Home "Your task" = clay, "Pick up a task" = mauve, "Build progress" = slate, "Crush" = rose (a warm frame around the blue blueprint); on /tasks, Recent decisions (now the Record link) = mauve, Waiting for review = clay.
- Inside a tone, `--color-muted`, `--color-todo` and `--color-review` are darker (light) or lighter (dark) so small text keeps at least 4.5:1. Measured on the concepts: lowest text contrast 4.87:1 light, 5.7:1 dark.
- Fields and inset boxes keep `--color-sheet`/`--color-field`, so form controls stay white-ish on a tint.

## 2. Dark mode
- Follows the system by default; the sidebar has a Light/Dark button (label, not icon only) that stores `cc-theme` in localStorage. A small script in `<head>` sets `data-theme` before paint so there is no flash.
- Dark overrides only tokens (paper `#10171d`, sheet `#18222a`, ink `#e4ebef`, accent `#8fb6cf`). No component gets its own dark rules except the sidebar (darker than the page) and the tone tints.
- The Crush render keeps its transparent PNG on the dark blueprint panel.

## 3. Bubbles
- 16 CSS-only circles rise from below the screen behind every panel (`.bubbles`, fixed, `z-index: 0`, `pointer-events: none`, `aria-hidden`). Only `transform` is animated. They never sit over text.
- **Off** when the person prefers reduced motion, when printing, and when they press the sidebar's Bubbles button (`cc-bubbles` in localStorage).
- This is the one allowed exception to "no border-radius": bubbles are round.
