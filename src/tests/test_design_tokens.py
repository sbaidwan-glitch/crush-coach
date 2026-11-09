"""docs/design.md: only tokens for color, spacing, type, radius and shadow; AA contrast."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
TOKENS = (STATIC / "styles" / "tokens.css").read_text()
STYLE = (STATIC / "style.css").read_text()
COMPONENTS = {p.name: p.read_text() for p in (TEMPLATES / "components").glob("*.html")}


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


def test_stylesheet_has_no_raw_colors_or_one_off_sizes():
    css = strip_comments(STYLE)
    css = re.sub(r"url\([^)]*\)", "", css)  # font paths

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), (
        "raw hex color: use a color token"
    )
    assert not re.findall(r"\b(?:rgb|rgba|hsl|hsla)\(", css), "raw color function"
    sizes = re.findall(r"(?<![\w.-])(\d*\.?\d+)(px|rem|em|pt)\b", css)
    # 1px borders are the documented exception; breakpoints live in tokens or @media.
    offenders = [f"{n}{u}" for n, u in sizes if f"{n}{u}" != "1px"]
    assert not offenders, f"one-off sizes, use a token: {sorted(set(offenders))}"
    assert "box-shadow" not in css and "text-shadow" not in css


def test_components_have_no_raw_colors():
    for name, source in COMPONENTS.items():
        assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", source), f"{name}: raw hex color"


def test_every_variable_in_use_is_a_token_or_set_inline():
    used = set(re.findall(r"var\((--[\w-]+)\)", STYLE))
    for source in COMPONENTS.values():
        used |= set(re.findall(r"var\((--[\w-]+)\)", source))
    defined = set(token_values()) | {
        "--total"
    }  # --total is set per gauge in its markup

    assert used <= defined, f"undefined variables: {sorted(used - defined)}"


def test_text_colors_meet_wcag_aa_on_both_surfaces():
    t = token_values()
    surfaces = [t["--color-bg"], t["--color-raised"]]

    for name in ("--color-ink", "--color-muted", "--color-coach", "--color-alert"):
        for surface in surfaces:
            assert contrast(t[name], surface) >= 4.5, f"{name} on {surface}"
    # Button text on the coach fill.
    assert contrast(t["--color-bg"], t["--color-coach"]) >= 4.5
    # Control edges use muted, which needs 3:1.
    assert contrast(t["--color-muted"], t["--color-bg"]) >= 3


def test_every_font_ships_with_its_open_font_license():
    fonts = STATIC / "fonts"
    licenses = {p.name: p.read_text() for p in fonts.glob("OFL-*.txt")}

    assert set(licenses) == {"OFL-redaction-35.txt", "OFL-hubot-sans.txt", "OFL-martian-mono.txt"}
    for name, text in licenses.items():
        assert "SIL Open Font License" in text, name
        assert "Copyright" in text, name
    # Every font file belongs to a licensed family.
    for font in fonts.glob("*.woff2"):
        family = font.name.rsplit("-", 1)[0]
        assert fonts / f"OFL-{family}.txt" in set(fonts.glob("OFL-*.txt")), font.name


def test_fonts_are_self_hosted():
    for font in (
        "redaction-35-400.woff2",
        "redaction-35-700.woff2",
        "hubot-sans-wdth.woff2",
        "martian-mono-wght.woff2",
    ):
        assert (STATIC / "fonts" / font).exists()
    assert "fonts.googleapis.com" not in STYLE
