"""docs/design.md and docs/theme.md ("Polar"): the shell, dark mode, the animation rules."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import home
import main
import progress
from models import Task, WorkNote

SRC = Path(__file__).resolve().parents[1]
STYLES = SRC / "static" / "styles"
THEME = (STYLES / "theme.css").read_text()
EXTRAS = (STYLES / "extras.css").read_text()
APP = (STYLES / "app.css").read_text()
SAM = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
LEAD = {"name": "Lee Example", "email": "lee@example.edu", "role": "lead"}


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


def nav_of(html: str) -> str:
    start = html.index('<aside class="sidebar">')
    return html[start : html.index("</aside>", start)]


# --- what was removed ---


def test_the_bubbles_and_panel_tones_are_gone_from_every_file():
    sources = [*SRC.glob("templates/**/*.html"), *SRC.glob("static/**/*.css"), *SRC.glob("static/*.js")]
    for path in sources:
        if path.name == "htmx.min.js":  # vendored: its events "bubble"
            continue
        text = path.read_text()
        assert "bubble" not in text.lower(), path.name
        assert "cc-bubbles" not in text, path.name
        assert "data-tone" not in text, path.name
    assert not hasattr(progress, "PANEL_TONES")
    assert not hasattr(progress.group_progress([])[0], "tone")


def test_panels_have_no_coloured_top_rule():
    panel = re.search(r"\n\.panel \{([^}]*)\}", APP).group(1)

    assert "border-top" not in panel
    assert ".panel--strong" not in APP
    # The one orange top rule on Home is the Available tasks panel; the tiles have theirs.
    assert re.findall(r"(\.[\w-]+) \{[^}]*border-top: 2px solid", THEME) == [".tile", ".panel--available"]


# --- the shell ---


def test_the_sidebar_is_numbered_and_leads_get_the_inbox_and_admin(as_user):
    html = as_user(LEAD).get("/").text
    side = nav_of(html)

    assert "CRUSH" in side and "Underwater robotics" in side and "MuddSub / Crush" in side
    labels = re.findall(r'<span class="k">(\d\d) / </span>([^<]+)</span>', side)
    assert labels == [
        ("01", "Home"), ("02", "Inbox"), ("03", "Tasks"), ("04", "Timeline"),
        ("05", "Notebook"), ("06", "Team record"),
    ]
    assert 'href="/notebook"' in side and 'href="/admin"' in side and ">Lead / Admin<" in side
    assert 'aria-current="page"' in side and side.count('aria-current="page"') == 1
    assert 'id="theme-btn"' in side and ">Dark<" in side
    assert 'class="sonar"' in side and 'aria-hidden="true"' in side


def test_a_member_has_no_inbox_or_admin_and_the_numbers_close_up(as_user):
    side = nav_of(as_user(SAM).get("/").text)

    labels = re.findall(r'<span class="k">(\d\d) / </span>([^<]+)</span>', side)
    assert [name for _, name in labels] == ["Home", "Tasks", "Timeline", "Notebook", "Team record"]
    assert [number for number, _ in labels] == ["01", "02", "03", "04", "05"]
    assert "/inbox" not in side and "/admin" not in side and "Lead / Admin" not in side


def test_the_tasks_link_shows_how_many_tasks_are_open(session, as_user):
    for code, status in (("M01", "todo"), ("M02", "in_progress"), ("M03", "done")):
        session.add(Task(code=code, title=code, subsystem="Enclosure", task_type="Design", stage="CAD", status=status))
    session.commit()

    side = nav_of(as_user(SAM).get("/tasks").text)

    assert '<span class="sidebar__count" aria-hidden="true">2</span>' in side
    assert ", 2 open" in side


def test_the_mode_button_replaces_the_bubbles_button(as_user):
    html = as_user(SAM).get("/").text

    assert "bubbles-btn" not in html and "Bubbles" not in html
    assert 'class="bubbles"' not in html
    assert "theme.js" in html


def test_the_head_script_sets_the_theme_before_the_stylesheets_paint(as_user):
    html = as_user(SAM).get("/").text

    head = html[: html.index("</head>")]
    script = head[head.index("<script>") :]
    assert 'localStorage.getItem("cc-theme")' in script and "dataset.theme" in script
    css = re.findall(r"styles/(\w+)\.css", head)
    assert css == ["tokens", "app", "theme", "timeline", "extras"]


def test_theme_js_keeps_the_cc_theme_key_and_follows_the_system_until_pressed():
    source = (SRC / "static" / "theme.js").read_text()

    assert '"cc-theme"' in source and "prefers-color-scheme: dark" in source
    assert "bubbles" not in source


def test_phones_get_a_bottom_tab_bar_of_labels(as_user):
    # The number and the count columns are hidden under 820px; the bar scrolls sideways.
    phone = THEME[THEME.index("@media (max-width: 820px) {") :]
    assert ".k, .sidebar__badge, .sidebar__count { display: none; }" in phone
    assert "position: fixed; inset: auto 0 0 0" in phone and "overflow-x: auto" in phone


# --- dark mode ---


def test_dark_follows_the_system_and_the_choice_overrides_it():
    assert ':root[data-theme="dark"] {' in THEME
    assert "@media (prefers-color-scheme: dark)" in THEME
    assert ':root:not([data-theme="light"]) {' in THEME


def test_both_dark_blocks_set_the_same_tokens():
    explicit = re.search(r':root\[data-theme="dark"\] \{([^}]*)\}', THEME).group(1)
    system = re.search(r':root:not\(\[data-theme="light"\]\) \{([^}]*)\}', THEME).group(1)

    def pairs(block):
        return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))

    assert pairs(explicit) == pairs(system)


# --- the animation rules ---


def test_the_nudge_is_not_printed_and_is_a_small_prompt_not_a_modal():
    nudge = re.search(r"\n\.nudge \{([^}]*)\}", THEME).group(1)

    assert "position: fixed" in nudge and "inset: 0" not in nudge  # a corner prompt, no backdrop
    assert "@media print { .nudge { display: none !important; } }" in THEME
    assert "backdrop" not in THEME.split(".nudge")[1].split("/* Entrance")[0]


def test_every_animation_is_off_for_reduced_motion_and_print():
    for query in ("@media (prefers-reduced-motion: reduce)", "@media print"):
        block = THEME[THEME.rindex(query) :]  # the closing rules at the end of theme.css
        block = block[: block.index("\n}")]
        assert "animation: none !important" in block, query
    # Loops are CSS only (no JS timers) and none starts the page hidden: the final state is
    # the normal state, the animation only runs "from" something.
    assert "setInterval" not in (SRC / "static" / "theme.js").read_text()
    assert "stroke-dashoffset: var(--off" in THEME and "@keyframes fill { from { stroke-dashoffset: 439.8; } }" in THEME
    assert "@keyframes grow { from { transform: scaleX(0); } }" in THEME
    assert "@keyframes rise { from { opacity: 0;" in THEME
    assert "opacity: 0;\n" not in re.sub(r"@keyframes[^{]*\{[^\n]*\n", "", THEME)


# --- Home ---


def home_html(session, as_user, user=SAM):
    return as_user(user).get("/").text


def test_home_has_the_three_tiles_and_the_grid(session, as_user):
    html = home_html(session, as_user)

    for heading in ("Notes this week", "Your tasks in review", "Available tasks"):
        assert heading in html
    assert html.index("Notes this week") < html.index('id="crush"') < html.index('id="active-task"') < html.index('id="available"')
    assert "Make today" not in html and "Open notebook" not in html  # no bottom banner
    assert "Log today&rsquo;s work" in html
    assert 'class="panel panel--navy' in html and 'class="panel panel--available' in html


def test_the_gauge_arc_comes_from_the_real_percentage(session, as_user):
    for i in range(4):
        session.add(Task(code=f"M0{i}", title="t", subsystem="Enclosure", task_type="Design", stage="CAD", status="done" if i == 0 else "todo"))
    session.commit()

    html = home_html(session, as_user)

    # 1 of 4 done = 25%: 439.8 * 0.75 is left unfilled.
    assert "--off:329.9" in html and "25%" in html and 'aria-label="Build 25 percent complete"' in html
    assert ">9%<" not in html


def test_the_decorations_are_hidden_from_screen_readers(session, as_user):
    html = home_html(session, as_user)

    assert '<div class="waves" aria-hidden="true">' not in html  # no active task: no waves
    side = nav_of(html)
    assert '<svg class="sonar"' in side and 'aria-hidden="true"' in side.split('<svg class="sonar"')[1].split(">")[0]


def test_the_waves_sit_in_the_active_task_panel_and_are_decorative(session, as_user):
    session.add(Task(code="E01", title="Mine", subsystem="Thrusters", task_type="Print", stage="Check CAD", status="in_progress", owner="Sam Example"))
    session.commit()

    html = home_html(session, as_user)

    assert '<div class="waves" aria-hidden="true"></div>' in html
    assert (STYLES.parent / "img" / "waves.svg").exists()


def test_the_notes_tile_counts_published_notes_only_and_your_drafts(session, as_user):
    def note(state, who="sam@example.edu"):
        session.add(WorkNote(member=who, member_name="x", task_code="M01", subsystem="Enclosure", did="d", original="d", state=state))

    note("published", "other@example.edu")
    note("published")
    note("draft")
    note("draft", "other@example.edu")  # someone else's draft is never counted
    session.commit()

    html = home_html(session, as_user)

    tile = html[html.index("Notes this week") - 200 : html.index("Notes this week") + 200]
    assert ">2</span>" in tile and "1 draft of yours" in tile


def test_without_drafts_the_tile_has_no_second_clause(session, as_user):
    html = home_html(session, as_user)

    assert "draft of yours" not in html and "drafts of yours" not in html


def test_the_tokens_and_components_have_four_pixel_corners():
    assert "--radius: 4px" in (STYLES / "tokens.css").read_text()
    for sheet in (APP, EXTRAS, (STYLES / "timeline.css").read_text()):
        # (the square and diamond status marks are ::before shapes and stay square)
        sheet = re.sub(r"[^{}]*::before\s*\{[^}]*\}", "", sheet)
        assert not re.search(r"border-radius:\s*0\s*[;}]", sheet)
