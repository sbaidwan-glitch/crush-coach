# Theme: Polar tokens, dark mode, motion

Reference: `docs/concepts/polar.html` / `polar.css` and the four `polar-*.png` screenshots (source of truth). Rules: `docs/design.md`.

## 1. Tokens
- `src/static/styles/tokens.css` is the light theme. Its base tokens are the concept's names: `--page`, `--paper`, `--paper-line`, `--field`, `--track`, `--tile*`, `--panel*`, `--ink`, `--muted`, `--orange`, `--on-orange`, `--orange-text-on-page`, `--orange-text-on-panel`, `--side*`, plus status colours `--info`, `--ok`, `--danger` (and `*-on-panel`).
- The `--color-*` names pages already use (`--color-paper`, `--color-sheet`, `--color-ink`, `--color-accent`, …) are aliases of those, so a theme only changes the base tokens. Keep using the `--color-*` names in markup; use the base names for the Polar-specific pieces.
- `--radius` is 4px. `--focus-ring` is a 2px orange ring for the few places an outline can't be used; everywhere else focus is `outline: 2px solid var(--orange)`.
- `extras.css` may only use tokens (`tests/test_design_tokens.py` rejects raw hex, one-off sizes and any `border-radius` that isn't `var(--radius)`). `theme.css` may hold raw values for the dark theme and the Home decorations.

## 2. Dark mode
- Follows the system by default; the sidebar's Light/Dark button stores `cc-theme` (`light` or `dark`) in localStorage. A small script in `<head>` sets `data-theme` before paint, so there is no flash. The button's label names the mode you would switch to.
- `theme.css` overrides only the base tokens, twice: `:root[data-theme="dark"]` and the same block inside `@media (prefers-color-scheme: dark)` for `:root:not([data-theme="light"])`. A test checks the two blocks match.
- Dark: page `#08111c`, paper `#0e1b2c`, panel `#122338`, ink `#eaf1f6`, orange `#f08a4f`. Navy panels get a 1px `--panel-rule` border in dark.
- The Crush render keeps its transparent PNG on the blueprint grid.

## 3. Navy panels
`.panel--navy` re-points `--color-sheet`, `--color-ink`, `--color-muted`, `--color-rule`, `--color-field`, `--color-accent`, status colours and `--color-on-ink` to the panel set. Anything inside it that uses the `--color-*` names is therefore correct on navy with no extra rules. Buttons inside get the `--panel-btn` fill; `.btn--primary` stays orange.

## 4. Motion
All CSS, all decorative: sonar sweep (sidebar), drifting waves (Active task panel, `img/waves.svg`), Crush's bob, the gauge's tick ring, the entrance fade-up (`.rise`), and the one-time fills of the gauge arc and the subsystem bars.
- The gauge arc is a circle of circumference 439.8 (r = 70). Its unfilled length is `--off`, computed in `home.html` from the real percentage; the animation only runs *from* 439.8. Bars are `width: done/total` and animate `scaleX` *from* 0. So with animation off they are at their final state.
- `@media (prefers-reduced-motion: reduce)` and `@media print` both set `animation: none !important` on everything. Decorative elements are `aria-hidden`.
- There is no Bubbles layer, button, CSS, JS or `cc-bubbles` key (removed in the Polar redesign), and no panel tints (`data-tone`) or panel top rules.
