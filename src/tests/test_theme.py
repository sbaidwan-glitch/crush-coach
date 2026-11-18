"""docs/theme.md: a tone per panel, the Light/Dark and Bubbles buttons, bubbles behind the page."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import home
import main
import progress
from models import Task

STYLES = Path(__file__).resolve().parents[1] / "static" / "styles"
THEME = (STYLES / "theme.css").read_text()
EXTRAS = (STYLES / "extras.css").read_text()
SAM = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
LEAD = {"name": "Lee Example", "email": "lee@example.edu", "role": "lead"}
SUBSYSTEM_TONES = {"blue", "teal", "sage", "sand"}


@pytest.fixture
def as_user(monkeypatch):
    """A client signed in as whoever the test names."""

    def sign_in(user):
        main.app.dependency_overrides[auth.require_user] = lambda: user
        main.app.dependency_overrides[auth.optional_user] = lambda: user
        monkeypatch.setattr(auth, "session_user", lambda request: user)
        monkeypatch.setattr(home.auth, "session_user", lambda request: user)
        return TestClient(main.app)

    yield sign_in
    main.app.dependency_overrides.clear()


# --- the tone mapping ---


def test_every_part_of_crush_has_a_tone_and_the_four_are_different():
    slugs = [spec["slug"] for spec in progress.SUBSYSTEM_GROUPS]

    tones = [progress.PANEL_TONES[slug] for slug in slugs]

    assert tones == ["blue", "teal", "sage", "sand"]  # Enclosure, Seals, Thrusters, Actuators
    assert len(set(tones)) == 4


def test_other_panels_never_use_a_subsystem_tone():
    slugs = {spec["slug"] for spec in progress.SUBSYSTEM_GROUPS}

    others = {key: tone for key, tone in progress.PANEL_TONES.items() if key not in slugs}

    assert others == {
        "your-task": "clay",
        "pick-up": "mauve",
        "build-progress": "slate",
        "crush": "rose",
    }
    assert not set(others.values()) & SUBSYSTEM_TONES


def test_every_tone_in_the_mapping_is_defined_in_theme_css_for_light_and_dark():
    for tone in set(progress.PANEL_TONES.values()):
        selector = f'.panel[data-tone="{tone}"]'
        # Once for light, once for the dark theme, once for dark following the system.
        assert THEME.count(selector) == 3, tone


def test_a_group_knows_its_tone():
    groups = progress.group_progress([])

    assert [g.tone for g in groups] == ["blue", "teal", "sage", "sand"]


def test_the_tasks_page_gives_each_subsystem_panel_its_tone(session, as_user):
    for code, subsystem in (
        ("M01", "Enclosure"),
        ("M02", "Seals/Waterproofing"),
        ("M03", "Thrusters"),
        ("G01", "Gripper"),
    ):
        session.add(
            Task(
                code=code,
                title=f"{subsystem} thing",
                subsystem=subsystem,
                task_type="Design",
                stage="CAD",
            )
        )
    session.commit()

    html = as_user(SAM).get("/tasks").text

    for slug, tone in (
        ("enclosure", "blue"),
        ("seals", "teal"),
        ("thrusters", "sage"),
        ("actuators", "sand"),
    ):
        assert f'data-tone="{tone}" id="{slug}"' in html


def test_home_panels_use_their_own_tones(session, as_user):
    html = as_user(SAM).get("/").text

    for heading, tone in (
        ("your-task", "clay"),
        ("pick-up", "mauve"),
        ("build-progress", "slate"),
        ("crush", "rose"),
    ):
        assert re.search(rf'data-tone="{tone}" aria-labelledby="{heading}"', html), heading


# --- the shell: head script, buttons, bubbles ---


def test_every_page_has_both_buttons_the_head_script_and_the_bubbles(as_user):
    html = as_user(SAM).get("/").text

    nav = html[html.index('<nav class="sidebar"') : html.index("</nav>")]
    assert 'id="theme-btn"' in nav and ">Dark<" in nav
    assert 'id="bubbles-btn"' in nav and ">Bubbles<" in nav and 'aria-pressed="true"' in nav
    assert html.count('<div class="bubbles" aria-hidden="true">') == 1
    assert html.count("<i style=") == 16
    assert "theme.js" in html


def test_the_head_script_sets_the_theme_before_the_stylesheets_paint(as_user):
    html = as_user(SAM).get("/").text

    head = html[: html.index("</head>")]
    script = head[head.index("<script>") :]
    assert 'localStorage.getItem("cc-theme")' in script and "dataset.theme" in script
    assert 'localStorage.getItem("cc-bubbles")' in script
    # theme.css comes after app.css (and before extras.css, whose phone rules size the buttons).
    css = [m for m in re.findall(r"styles/(\w+)\.css", head)]
    assert css == ["tokens", "app", "theme", "extras"]


def test_the_buttons_are_in_the_bottom_bar_on_phones(as_user):
    html = as_user(LEAD).get("/").text

    nav = html[html.index('<nav class="sidebar"') : html.index("</nav>")]
    # One bar holds the links and both buttons; extras.css lets them share the width.
    assert nav.count("sidebar__btn") == 2 and "Admin" in nav
    phone = re.search(r"@media \(max-width: 820px\) \{\s*\.sidebar__link, \.sidebar__btn \{([^}]*)\}", EXTRAS)
    assert phone and "flex: 1 1 0" in phone.group(1)


# --- stylesheets ---


def test_theme_js_stores_the_choice_under_the_names_the_head_script_reads():
    source = (STYLES.parent / "theme.js").read_text()

    assert '"cc-theme"' in source and '"cc-bubbles"' in source
    assert "prefers-color-scheme: dark" in source


def test_no_raw_colours_were_added_to_extras_or_the_templates():
    css = re.sub(r"/\*.*?\*/", "", EXTRAS, flags=re.DOTALL)

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.findall(r"\b(?:rgb|rgba|hsl|hsla)\(", css)
    base = (STYLES.parents[1] / "templates" / "base.html").read_text()
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", base)


def test_bubbles_are_the_only_round_shapes_and_stay_behind_the_panels():
    rounded = re.findall(r"([^{}]+)\{[^{}]*border-radius", THEME)

    assert [r.strip() for r in rounded] == [".bubbles i"]
    assert "pointer-events: none" in THEME
    assert "prefers-reduced-motion: reduce" in THEME and "@media print" in THEME
