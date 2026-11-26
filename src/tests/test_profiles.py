"""docs/timeline.md: member icons. The fixed list of nine, the name length, that members
edit only their own profile, and the chip next to the owner on task rows and the task page."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import main
import profiles
from models import MemberProfile, Task

RILEY = {"name": "Riley Sample", "email": "riley@example.edu", "role": "member"}
SAM = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}


@pytest.fixture
def as_user(monkeypatch):
    """A client signed in as whoever the test names (session and dependencies agree)."""

    def sign_in(user):
        main.app.dependency_overrides[auth.require_user] = lambda: user
        main.app.dependency_overrides[auth.optional_user] = lambda: user
        monkeypatch.setattr(auth, "session_user", lambda request: user)
        return TestClient(main.app)

    yield sign_in
    main.app.dependency_overrides.clear()


def stored(session, email):
    session.expire_all()
    return session.get(MemberProfile, email)


def add_task(session, code, owner, subsystem="Enclosure", status="in_progress"):
    task = Task(code=code, title=f"Task {code}", subsystem=subsystem, task_type="Fabricate",
                stage="drawing", status=status, owner=owner)
    session.add(task)
    session.commit()
    return task


# --- the list, the name, and the pure helpers ---


def test_there_are_exactly_nine_icons_and_the_default_is_the_gear():
    assert profiles.ICONS == (
        "submarine", "drill", "octopus", "claw", "torpedo", "wrench", "anchor", "gear", "fish")
    assert profiles.DEFAULT_ICON == "gear" and profiles.MAX_NAME == 20


def test_every_icon_in_the_list_is_drawn_in_the_sprite(session, as_user):
    html = as_user(RILEY).get("/profile").text

    for name in profiles.ICONS:
        assert f'<symbol id="av-{name}"' in html


def test_a_name_is_tidied_and_limited_to_twenty_characters():
    assert profiles.clean_name("  Riley   S  ") == "Riley S"
    assert profiles.clean_name("") == ""
    assert profiles.clean_name("x" * 20) == "x" * 20
    with pytest.raises(profiles.ProfileError):
        profiles.clean_name("x" * 21)
    with pytest.raises(profiles.ProfileError):
        profiles.clean_name("Riley\x00")


def test_an_icon_outside_the_list_is_refused():
    assert profiles.clean_icon("fish") == "fish"
    for bad in ("", "Fish", "shark", "../etc", "gear "):
        with pytest.raises(profiles.ProfileError):
            profiles.clean_icon(bad)


def test_owner_cells_with_several_people_are_split():
    assert profiles.owner_names("Sam Example, Riley Sample") == ["Sam Example", "Riley Sample"]
    assert profiles.owner_names("Sam Example & Riley Sample") == ["Sam Example", "Riley Sample"]
    assert profiles.owner_names("Sam Example and Riley Sample") == ["Sam Example", "Riley Sample"]
    assert profiles.owner_names("Sam Example, sam example") == ["Sam Example"]
    assert profiles.owner_names("Alexandra Anderson") == ["Alexandra Anderson"]  # "and" inside a name
    assert profiles.owner_names(None) == [] and profiles.owner_names("  ") == []


def test_until_a_member_picks_they_show_as_their_first_name_with_the_gear():
    people = profiles.Profiles()

    assert people.chips("Riley Sample") == [profiles.Chip("gear", "Riley")]
    assert people.chips(None) == []


def test_a_pick_is_found_by_the_name_a_task_was_claimed_with_or_by_email():
    pick = MemberProfile(email="riley@example.edu", icon="fish", display_name="Rye",
                         full_name="Riley Sample")
    people = profiles.Profiles([pick])

    assert people.chips("riley sample") == [profiles.Chip("fish", "Rye")]
    assert people.chips("Riley@Example.edu") == [profiles.Chip("fish", "Rye")]
    assert people.chips("Riley Sample, Sam Example") == [
        profiles.Chip("fish", "Rye"), profiles.Chip("gear", "Sam")]
    assert profiles.chip_label(people.chips("Riley Sample, Sam Example")) == "Rye +1"


def test_a_pick_with_no_name_shows_the_first_name_and_an_unknown_icon_the_gear():
    people = profiles.Profiles([
        MemberProfile(email="a@example.edu", icon="anchor", display_name="", full_name="Ada Lovelace"),
        MemberProfile(email="b@example.edu", icon="removed-icon", display_name="B", full_name="Bo Peep"),
    ])

    assert people.chip("Ada Lovelace") == profiles.Chip("anchor", "Ada")
    assert people.chip("a@example.edu") == profiles.Chip("anchor", "Ada")  # never the address
    assert people.chip("Bo Peep") == profiles.Chip("gear", "B")


# --- /profile ---


def test_the_page_offers_the_nine_icons_and_the_default_is_checked(session, as_user):
    html = as_user(RILEY).get("/profile").text

    assert len(re.findall(r'<input type="radio" name="icon"', html)) == 9
    assert re.search(r'value="gear" checked', html)
    assert 'maxlength="20"' in html and 'placeholder="Riley"' in html


def test_saving_stores_the_pick_and_shows_it_next_time(session, as_user):
    client = as_user(RILEY)

    response = client.post("/profile", data={"icon": "octopus", "display_name": "  Rye "},
                           follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/profile?saved=1"
    row = stored(session, RILEY["email"])
    assert (row.icon, row.display_name, row.full_name) == ("octopus", "Rye", "Riley Sample")
    page = client.get("/profile?saved=1").text
    assert re.search(r'value="octopus" checked', page) and 'value="Rye"' in page
    assert "Saved." in page


def test_saving_again_changes_the_one_row_instead_of_adding_another(session, as_user):
    client = as_user(RILEY)
    client.post("/profile", data={"icon": "fish", "display_name": "A"})
    client.post("/profile", data={"icon": "claw", "display_name": ""})

    rows = session.exec(select(MemberProfile)).all()

    assert len(rows) == 1 and (rows[0].icon, rows[0].display_name) == ("claw", "")


def test_an_icon_outside_the_nine_is_refused_and_nothing_is_saved(session, as_user):
    response = as_user(RILEY).post("/profile", data={"icon": "shark", "display_name": "Rye"})

    assert response.status_code == 400
    assert "Pick one of the nine icons" in response.text
    assert stored(session, RILEY["email"]) is None


def test_a_name_over_twenty_characters_is_refused_and_the_form_keeps_what_was_typed(session, as_user):
    long_name = "A" * 21

    response = as_user(RILEY).post("/profile", data={"icon": "fish", "display_name": long_name})

    assert response.status_code == 400
    assert "20 characters or fewer" in response.text
    assert f'value="{long_name}"' in response.text and re.search(r'value="fish" checked', response.text)
    assert stored(session, RILEY["email"]) is None


def test_a_refused_save_leaves_an_earlier_pick_alone(session, as_user):
    client = as_user(RILEY)
    client.post("/profile", data={"icon": "fish", "display_name": "Rye"})

    client.post("/profile", data={"icon": "fish", "display_name": "B" * 30})

    assert stored(session, RILEY["email"]).display_name == "Rye"


def test_a_member_can_only_change_their_own_profile(session, as_user):
    as_user(SAM).post("/profile", data={"icon": "anchor", "display_name": "Sammy"})

    # Riley tries to write Sam's row by adding the email, the name or an id to the form.
    as_user(RILEY).post("/profile", data={
        "icon": "fish", "display_name": "Mine", "email": SAM["email"],
        "full_name": SAM["name"], "id": SAM["email"]})

    sam, riley = stored(session, SAM["email"]), stored(session, RILEY["email"])
    assert (sam.icon, sam.display_name, sam.full_name) == ("anchor", "Sammy", "Sam Example")
    assert (riley.icon, riley.display_name, riley.full_name) == ("fish", "Mine", "Riley Sample")


def test_the_page_has_no_field_that_names_whose_profile_it_is(session, as_user):
    html = as_user(RILEY).get("/profile").text
    form = html[html.index("<form", html.index("pick-heading")) :]
    form = form[: form.index("</form>")]

    assert set(re.findall(r'name="(\w+)"', form)) == {"icon", "display_name"}


def test_a_signed_out_visitor_is_sent_to_sign_in_and_cannot_save(session):
    main.app.dependency_overrides.clear()
    client = TestClient(main.app)

    page = client.get("/profile", follow_redirects=False)
    post = client.post("/profile", data={"icon": "fish", "display_name": "x"}, follow_redirects=False)

    assert page.status_code == 303 and page.headers["location"].startswith("/login")
    assert post.status_code == 303 and post.headers["location"].startswith("/login")
    assert session.exec(select(MemberProfile)).all() == []


def test_a_database_failure_while_saving_says_so_and_saves_nothing(session, as_user, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("down: secret@host")

    monkeypatch.setattr(profiles, "save", fail)

    response = as_user(RILEY).post("/profile", data={"icon": "fish", "display_name": "x"})

    assert response.status_code == 503
    assert "Nothing was saved" in response.text and "secret@host" not in response.text


def test_the_top_bar_chip_opens_the_profile_and_shows_the_members_icon(session, as_user):
    client = as_user(RILEY)
    client.post("/profile", data={"icon": "torpedo", "display_name": ""})

    html = client.get("/tasks").text

    assert re.search(r'<a class="chip-link" href="/profile"[^>]*>.*?#av-torpedo', html, re.DOTALL)
    assert "Signed in as Riley Sample" in html


# --- the chip next to the owner ---


def test_task_rows_show_a_chip_beside_the_owner_and_nothing_for_an_unclaimed_task(session, as_user):
    add_task(session, "M01", "Riley Sample")
    add_task(session, "M02", None, status="todo")
    as_user(RILEY).post("/profile", data={"icon": "wrench", "display_name": ""})
    client = as_user(SAM)

    html = client.get("/tasks").text

    owned = html[html.index("M01") : html.index("M02")]
    assert "#av-wrench" in owned and "Riley Sample" in owned
    unclaimed = html[html.index("M02") :]
    assert "Unclaimed" in unclaimed.split("</section>")[0] and "#av-" not in unclaimed.split("</section>")[0]


def test_a_member_who_has_not_picked_shows_the_gear_on_their_tasks(session, as_user):
    add_task(session, "M01", "Sam Example")

    html = as_user(RILEY).get("/tasks").text

    assert "#av-gear" in html[html.index("M01") :]


def test_the_task_page_shows_the_chip_next_to_the_owner(session, as_user):
    add_task(session, "M01", "Riley Sample")
    as_user(RILEY).post("/profile", data={"icon": "drill", "display_name": ""})

    html = as_user(SAM).get("/tasks/M01").text

    owner = html[html.index("field-label\">Owner") :]
    owner = owner[: owner.index("</div>", owner.index("field--row"))]
    assert "#av-drill" in owner and "Riley Sample" in owner


def test_the_signed_out_preview_shows_no_chips_and_no_names(session):
    add_task(session, "M01", "Riley Sample")
    main.app.dependency_overrides.clear()

    html = TestClient(main.app).get("/tasks").text

    assert "Riley" not in html
    row = html[html.index("M01") : html.index("</section>", html.index("M01"))]
    assert 'class="avatar"' not in row
