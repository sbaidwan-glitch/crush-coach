"""docs/design.md: tokens for colour, size and type; self-hosted fonts; AA contrast."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
STYLES = STATIC / "styles"
TOKENS = (STYLES / "tokens.css").read_text()
APP = (STYLES / "app.css").read_text()
EXTRAS = (STYLES / "extras.css").read_text()
COMPONENTS = {p.name: p.read_text() for p in (TEMPLATES / "components").glob("*.html")}
# Set inline in the markup (a progress bar's numbers, a legend key), not in tokens.css.
SET_INLINE = {"--c", "--n", "--done", "--started", "--k", "--col", "--tone", "--cols", "--d", "--off"}


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


THEME = (STYLES / "theme.css").read_text()


def token_values() -> dict[str, str]:
    body = strip_comments(TOKENS)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))


def dark_overrides() -> dict[str, str]:
    """The base tokens theme.css changes for dark (the `[data-theme="dark"]` block)."""
    block = re.search(r':root\[data-theme="dark"\]\s*\{([^}]*)\}', strip_comments(THEME))
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block.group(1)))


def resolved(theme: str) -> dict[str, str]:
    """Every token as a plain value, `var(--x)` aliases followed, for "light" or "dark"."""
    values = token_values()
    if theme == "dark":
        values.update(dark_overrides())

    def follow(name: str) -> str:
        value = values[name].strip()
        while value.startswith("var("):
            value = values[value[4:-1]].strip()
        return value

    return {name: follow(name) for name in values}


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_extras_stylesheet_uses_only_tokens():
    """extras.css is the one stylesheet we write: no raw colours, no one-off sizes."""
    css = strip_comments(EXTRAS)
    css = re.sub(r"@media[^{]*", "", css)  # breakpoints live in @media

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), "raw hex colour: use a token"
    assert not re.findall(r"\b(?:rgb|rgba|hsl|hsla)\(", css), "raw colour function"
    sizes = re.findall(r"(?<![\w.-])(\d*\.?\d+)(px|rem|em|pt)\b", css)
    offenders = [f"{n}{u}" for n, u in sizes if f"{n}{u}" != "1px"]
    assert not offenders, f"one-off sizes, use a token: {sorted(set(offenders))}"
    assert "box-shadow" not in css and "text-shadow" not in css
    # Rounded corners are 4px (--radius) on panels, buttons, tags and fields: never 0, never a
    # pill. (The square and diamond status marks, drawn with ::before, stay square on purpose.)
    css = re.sub(r"[^{}]*::before\s*\{[^}]*\}", "", css)
    assert not re.findall(r"border-radius\s*:\s*(?!\s|var\(--radius\)\s*[;}])[^;}]+", css), "use var(--radius)"


def test_templates_and_components_have_no_raw_colours():
    for path in (*TEMPLATES.glob("*.html"), *(TEMPLATES / "components").glob("*.html")):
        source = path.read_text()
        assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", source), f"{path.name}: raw hex"


def test_every_variable_in_use_is_a_token_or_set_inline():
    used = set(re.findall(r"var\((--[\w-]+)", APP + EXTRAS))
    for path in (*TEMPLATES.glob("*.html"), *(TEMPLATES / "components").glob("*.html")):
        used |= set(re.findall(r"var\((--[\w-]+)", path.read_text()))
    # A name ending in "-" is built in the markup (--color-sub-{{ n }}): check the stem.
    tokens = set(token_values())
    used = {u for u in used if not (u.endswith("-") and any(t.startswith(u) for t in tokens))}
    defined = tokens | SET_INLINE

    assert used <= defined, f"undefined variables: {sorted(used - defined)}"


# Every text/background pair the app uses, as (text, background, what it is, smallest allowed).
# docs/design.md has the same table, measured. Large text needs 3:1; everything here is 4.5:1.
CONTRAST_PAIRS = [
    ("ink", "page", "Body text on the page"),
    ("muted", "page", "Muted text on the page"),
    ("orange-text-on-page", "page", "Eyebrow, links on the page"),
    ("ink", "paper", "Text on a paper panel"),
    ("muted", "paper", "Muted text on paper"),
    ("orange-text-on-page", "paper", "Links and the review mark on paper"),
    ("info", "paper", "In-progress mark on paper"),
    ("ok", "paper", "Done mark on paper"),
    ("danger", "paper", "Error text on paper"),
    ("ink", "field", "Text in a field"),
    ("muted", "field", "Muted text in a field"),
    ("orange-text-on-page", "field", "Link in a field"),
    ("tile-ink", "tile", "Tile and banner text, links on a tile"),
    ("tile-muted", "tile", "Tile and banner secondary text"),
    ("muted", "tile", "Muted text in a Next-step box"),
    ("panel-ink", "panel", "Text on a navy panel"),
    ("panel-muted", "panel", "Muted text on a navy panel"),
    ("orange-text-on-panel", "panel", "Accent text on a navy panel"),
    ("info-on-panel", "panel", "In-progress mark on navy"),
    ("ok-on-panel", "panel", "Done mark on navy"),
    ("danger-on-panel", "panel", "Error on navy"),
    ("panel-ink", "panel-inset", "Text in a navy inset"),
    ("panel-muted", "panel-inset", "Muted text in a navy inset (steps, pills)"),
    ("orange-text-on-panel", "panel-inset", "Accent in a navy inset"),
    ("panel-ink", "panel-btn", "Secondary button on navy"),
    ("on-orange", "orange", "Primary button, active nav item, current step"),
    ("side-ink", "side", "Sidebar links"),
    ("panel-ink", "side", "Wordmark"),
    ("orange-text-on-panel", "side", "Mode button"),
    ("on-ink", "ink", "Text on an ink fill (selected filter, skip link)"),
]


def test_every_text_and_background_pair_meets_wcag_aa_in_light_and_dark():
    for theme in ("light", "dark"):
        t = resolved(theme)
        for fg, bg, what in CONTRAST_PAIRS:
            ratio = contrast(t[f"--{fg}"], t[f"--{bg}"])
            assert ratio >= 4.5, f"{theme}: {what} ({fg} on {bg}) is {ratio:.2f}:1"


def test_white_on_orange_is_not_used_for_buttons():
    """White on the orange fails 4.5:1; the primary button takes the dark navy."""
    for theme in ("light", "dark"):
        t = resolved(theme)
        assert contrast("#ffffff", t["--orange"]) < 4.5
        assert contrast(t["--on-orange"], t["--orange"]) >= 4.5
    assert "color: var(--on-orange)" in APP


def test_the_pages_names_all_resolve_in_both_themes():
    """Every --color-* name the pages use ends at a colour value, in both themes."""
    for theme in ("light", "dark"):
        for name, value in resolved(theme).items():
            if name.startswith("--color-") and not name.startswith("--color-sub-"):
                assert re.fullmatch(r"#[0-9a-f]{6}|rgba?\(.*\)", value), (theme, name, value)


def test_keyboard_focus_is_a_2px_orange_outline_on_every_control():
    assert ":focus-visible { outline: 2px solid var(--orange); outline-offset: 2px; }" in APP
    assert "outline: none" not in APP and "outline: none" not in THEME


def test_only_manrope_and_jetbrains_mono_ship_with_their_licences():
    fonts = STATIC / "fonts"

    assert sorted(p.name for p in fonts.iterdir() if p.is_dir()) == [
        "jetbrains-mono",
        "manrope",
    ]
    for family in ("manrope", "jetbrains-mono"):
        licence = (fonts / family / "OFL.txt").read_text()
        assert "SIL Open Font License" in licence and "Copyright" in licence
    # Nothing else sits loose in the fonts folder (the old Michroma, Space Mono, Hubot Sans).
    assert not list(fonts.glob("*.woff2")) and not list(fonts.glob("OFL-*.txt"))


def test_fonts_are_self_hosted_and_every_file_the_css_names_exists():
    css = TOKENS + APP + EXTRAS + THEME
    assert "fonts.googleapis.com" not in css
    for family in ("Michroma", "Space Mono", "Hubot Sans"):
        assert family not in css
    for sheet_name, sheet in (("tokens.css", TOKENS), ("app.css", APP), ("theme.css", THEME)):
        for url in re.findall(r"url\(\"?([^)\"]+)\"?\)", sheet):
            assert (STYLES / url).resolve().exists(), f"{sheet_name}: {url} is missing"


def test_every_variable_in_use_is_defined_in_light_and_nothing_else_is_unused_by_name():
    """Dark only overrides tokens that exist in the light theme."""
    assert set(dark_overrides()) <= set(token_values()) | {f"--color-sub-{n}" for n in range(1, 6)}


def test_the_old_stylesheet_is_gone_and_pages_link_the_new_ones():
    assert not (STATIC / "style.css").exists()
    base = (TEMPLATES / "base.html").read_text()
    for name in ("tokens.css", "app.css", "extras.css"):
        assert f"static_url('styles/{name}')" in base
    assert "style.css" not in base
