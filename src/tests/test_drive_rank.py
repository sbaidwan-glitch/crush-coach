"""Which results rank lower: config phrases, old seasons (all dates made up)."""

import logging
from datetime import date, datetime

import pytest

import drive_rank as rank

TODAY = date(2026, 11, 11)  # the 2026-2027 season is under way


def when(y, m, d=1):
    return datetime(y, m, d)  # noqa: DTZ001 (naive UTC, how the database stores times)


@pytest.fixture
def rules():
    return rank.load_rules(environ={})  # the committed drive_index_demote.txt


def old(rules, path, modified=None):
    return rules.assess(path, modified, TODAY).reasons


# --- phrases from the config file ---


def test_the_committed_list_has_the_words_from_the_team():
    rules = rank.load_rules(environ={})
    assert [text for text, _ in rules.phrases] == [
        "OBSOLETE",
        "Old",
        "Archive*",
        "Alfie",
        "for Comparison",
    ]
    assert (rules.season_start_month, rules.old_after_seasons) == (8, 1)


@pytest.mark.parametrize(
    "path",
    [
        "MuddSub / Mechanical / OBSOLETE",
        "MuddSub / Mechanical / obsolete drawings",
        "MuddSub / Mechanical / CAD / Old Crush",
        "MuddSub / Mechanical / old_notes",
        "MuddSub / Mechanical / Archive",
        "MuddSub / Mechanical / Archived 2023",
        "MuddSub / Mechanical / Alfie_CAD",
        "MuddSub / Mechanical / Simplified Alfie for Comparison / Parts",
        "MuddSub / SEMI-SORTED OLD MATERIALS / Learning",
    ],
)
def test_these_folder_paths_are_demoted(rules, path):
    assert rules.assess(path, None, TODAY).folder_words


@pytest.mark.parametrize(
    "path",
    [
        "MuddSub / Mechanical / Camera Holder + Eyeholes",  # "old" inside a word
        "MuddSub / Mechanical / Gold plating",
        "MuddSub / Mechanical / Bold",
        "MuddSub / Mechanical / Comparisons",  # not the phrase
        "MuddSub / Mechanical / CAD / Finalized Enclosure",
    ],
)
def test_whole_words_only_so_holder_is_not_old(rules, path):
    assert not rules.assess(path, None, TODAY).folder_words


def test_each_matching_phrase_is_reported(rules):
    found = rules.assess("A / Simplified Alfie for Comparison", None, TODAY)
    assert found.folder_words == ("Alfie", "for Comparison")
    assert found.reasons == (
        "folder path mentions Alfie",
        "folder path mentions for Comparison",
    )


# --- seasons ---


def test_the_current_season_starts_in_august(rules):
    assert rules.current_season(date(2026, 7, 31)) == 2025
    assert rules.current_season(date(2026, 8, 1)) == 2026
    assert rules.current_season(date(2027, 1, 15)) == 2026


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("MuddSub / 2026-2027 / Mechanical", None),
        ("MuddSub / 2025-2026 / Mechanical", None),  # last season is fine
        ("MuddSub / 2024-2025 / Mechanical", ("from the 2024-2025 season",)),
        ("MuddSub / 2021-2022 / Software", ("from the 2021-2022 season",)),
    ],
)
def test_a_season_folder_more_than_a_season_behind_is_old(rules, path, expected):
    assert (old(rules, path) or None) == expected


def test_season_spellings_and_look_alikes():
    r = rank.DemoteRules()
    assert r.season_of("A / 2024-25 / B", None) == 2024
    assert r.season_of("A / 2024 - 2025 / B", None) == 2024
    assert r.season_of("A / 2024/2025", None) == 2024
    assert r.season_of("A / 2024-2027 / B", None) is None  # not consecutive years
    assert r.season_of("A / 12024-2025 / B", None) is None
    assert r.season_of("A / Parts", None) is None


def test_a_file_outside_any_season_folder_is_placed_by_its_modified_date(rules):
    assert old(rules, "MuddSub / Misc", when(2021, 6, 19)) == (
        "from the 2020-2021 season",
    )
    assert old(rules, "MuddSub / Misc", when(2026, 9, 1)) == ()
    assert old(rules, "MuddSub / Misc", when(2026, 3, 1)) == ()  # the 2025-2026 season
    assert old(rules, "MuddSub / Misc", None) == ()  # no folder, no date: not guessed


def test_the_season_folder_beats_a_recent_modified_date(rules):
    # an old-season file somebody edited last week is still an old-season file
    assert old(rules, "MuddSub / 2021-2022 / Crush", when(2026, 11, 1)) == (
        "from the 2021-2022 season",
    )


def test_old_after_seasons_is_configurable():
    strict = rank.parse_rules(["old_after_seasons = 0"])
    assert strict.assess("A / 2025-2026", None, TODAY).old_season == 2025
    lenient = rank.parse_rules(["old_after_seasons = 3"])
    assert lenient.assess("A / 2023-2024", None, TODAY).old_season is None


def test_both_kinds_of_reason_can_apply_at_once(rules):
    found = rules.assess("MuddSub / 2021-2022 / Alfie", None, TODAY)
    assert found.folder_words == ("Alfie",) and found.old_season == 2021
    assert len(found.reasons) == 2


# --- loading ---


def test_rules_load_from_the_file_and_the_environment_variable(tmp_path):
    path = tmp_path / "demote.txt"
    path.write_text("# mine\nObsolete\nseason_start_month = 9\n")
    rules = rank.load_rules(
        path, {rank.ENV_VAR: "Scratch;Spring Break\nold_after_seasons = 2"}
    )
    assert [text for text, _ in rules.phrases] == [
        "Obsolete",
        "Scratch",
        "Spring Break",
    ]
    assert (rules.season_start_month, rules.old_after_seasons) == (9, 2)


def test_a_missing_file_or_a_bad_setting_is_a_warning_not_a_failure(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="crush_coach"):
        rules = rank.load_rules(tmp_path / "nope.txt", {rank.ENV_VAR: "Scratch"})
        bad = rank.parse_rules(["season_start_month = 13", "old_after_seasons = lots"])
    assert [text for text, _ in rules.phrases] == ["Scratch"]
    assert "could not read" in caplog.text and "bad setting" in caplog.text
    assert bad.season_start_month == 8 and bad.old_after_seasons == 1
