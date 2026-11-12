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
SET_INLINE = {"--c", "--n", "--done", "--started", "--k"}


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def token_values() -> dict[str, str]:
    body = strip_comments(TOKENS)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))


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
    assert not re.findall(r"border-radius\s*:\s*(?!0)[^\s;]", css), "square corners only"


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


def test_text_colours_meet_wcag_aa_where_the_design_says_they_do():
    t = token_values()

    for name in ("--color-ink", "--color-muted", "--color-accent", "--color-done"):
        assert contrast(t[name], t["--color-sheet"]) >= 4.5, f"{name} on a panel"
    assert contrast(t["--color-ink"], t["--color-paper"]) >= 4.5
    assert contrast(t["--color-on-ink"], t["--color-ink"]) >= 4.5  # primary buttons
    assert contrast(t["--color-on-ink-muted"], t["--color-ink"]) >= 4.5  # sidebar


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
    css = TOKENS + APP + EXTRAS
    assert "fonts.googleapis.com" not in css
    for family in ("Michroma", "Space Mono", "Hubot Sans"):
        assert family not in css
    for sheet_name, sheet in (("tokens.css", TOKENS), ("app.css", APP)):
        for url in re.findall(r"url\(\"?([^)\"]+)\"?\)", sheet):
            assert (STYLES / url).resolve().exists(), f"{sheet_name}: {url} is missing"


def test_the_old_stylesheet_is_gone_and_pages_link_the_new_ones():
    assert not (STATIC / "style.css").exists()
    base = (TEMPLATES / "base.html").read_text()
    for name in ("tokens.css", "app.css", "extras.css"):
        assert f"/static/styles/{name}" in base
    assert "style.css" not in base
