"""Tests for the saved destinations library (config.destinations)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from noc_beam.config import destinations as dst


@pytest.fixture
def isolated_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the user destinations.json to a tmp_path so tests
    never touch the operator's real %APPDATA% file."""
    monkeypatch.setattr(dst, "_user_json_path", lambda: tmp_path / "destinations.json")
    return tmp_path


# ----------------------------------------------------------------------
# country_of()
# ----------------------------------------------------------------------
def test_country_of_simple_label() -> None:
    assert dst.country_of("Egypt") == "Egypt"


def test_country_of_hyphenated_label() -> None:
    assert dst.country_of("Egypt-Mobile (Vodafone)") == "Egypt"


def test_country_of_double_hyphen() -> None:
    assert dst.country_of("Egypt-Fix-Special Services") == "Egypt"


def test_country_of_no_hyphen_special() -> None:
    assert dst.country_of("Aeromobile") == "Aeromobile"


def test_country_of_strips_whitespace() -> None:
    assert dst.country_of(" Angola ") == "Angola"
    assert dst.country_of("Angola - Mobile (Africell)") == "Angola"


def test_country_of_empty() -> None:
    assert dst.country_of("") == ""
    assert dst.country_of("   ") == ""


# ----------------------------------------------------------------------
# Loader: bundled default + first-run seed
# ----------------------------------------------------------------------
def test_loads_bundled_default_when_user_file_absent(isolated_user_dir: Path) -> None:
    items = dst.load_destinations()
    # Seeded catalogue has 1,400 zones across 354 countries.
    assert len(items) == 1400
    assert len({d.country for d in items}) == 354


def test_first_run_seeds_user_file(isolated_user_dir: Path) -> None:
    user_path = isolated_user_dir / "destinations.json"
    assert not user_path.exists()
    items = dst.load_destinations()
    assert user_path.exists(), "first-run load should seed the user JSON"
    # File on disk matches what was returned.
    raw = json.loads(user_path.read_text(encoding="utf-8"))
    assert len(raw) == len(items)


def test_empty_numbers_rows_are_kept(isolated_user_dir: Path) -> None:
    """Seed file has all-empty numbers; loader must NOT skip them."""
    items = dst.load_destinations()
    # Every seeded row has empty numbers -- they should all survive.
    assert all(d.numbers == () for d in items)
    assert len(items) == 1400


# ----------------------------------------------------------------------
# Parser: invalid rows, duplicates
# ----------------------------------------------------------------------
def test_parse_skips_rows_missing_country_or_zone(
    isolated_user_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    user_path = isolated_user_dir / "destinations.json"
    user_path.write_text(
        json.dumps(
            [
                {"country": "Egypt", "zone": "Egypt", "numbers": ["20212345678"]},
                {"country": "Egypt", "numbers": ["111"]},       # missing zone
                {"zone": "Egypt-Mobile", "numbers": ["222"]},   # missing country
                {"country": "", "zone": "Z", "numbers": ["333"]},
                "not even a dict",
                {"country": "Egypt", "zone": "Egypt-Mobile (Vodafone)", "numbers": ["44"]},
            ]
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        items = dst.load_destinations()
    assert len(items) == 2
    keys = {(d.country, d.zone) for d in items}
    assert keys == {("Egypt", "Egypt"), ("Egypt", "Egypt-Mobile (Vodafone)")}
    # We expect at least one warning emitted for skipped rows.
    assert any("Skipping" in r.message or "skip" in r.message.lower()
               for r in caplog.records)


def test_parse_duplicate_country_zone_last_wins(
    isolated_user_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    user_path = isolated_user_dir / "destinations.json"
    user_path.write_text(
        json.dumps(
            [
                {"country": "Egypt", "zone": "Egypt", "numbers": ["111"]},
                {"country": "Egypt", "zone": "Egypt", "numbers": ["222", "333"]},
            ]
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        items = dst.load_destinations()
    assert len(items) == 1
    assert items[0].numbers == ("222", "333")
    assert any("Duplicate" in r.message for r in caplog.records)


def test_parse_empty_numbers_kept_no_warning(
    isolated_user_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    user_path = isolated_user_dir / "destinations.json"
    user_path.write_text(
        json.dumps([{"country": "Egypt", "zone": "Egypt", "numbers": []}]),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        items = dst.load_destinations()
    assert len(items) == 1
    assert items[0].numbers == ()
    # No warning specifically about empty numbers (the deviation from spec).
    assert not any("empty" in r.message.lower() and "number" in r.message.lower()
                   for r in caplog.records)


def test_parse_malformed_json_falls_back_to_default(
    isolated_user_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    user_path = isolated_user_dir / "destinations.json"
    user_path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        items = dst.load_destinations()
    # Falls back to bundled default -> 1400 rows.
    assert len(items) == 1400


# ----------------------------------------------------------------------
# countries / zones_for / zones_with_numbers / lookup
# ----------------------------------------------------------------------
def _sample_items() -> list[dst.Destination]:
    return [
        dst.Destination("Egypt", "Egypt", ("20212345678",)),
        dst.Destination("Egypt", "Egypt-Mobile (Vodafone)", ("201001234567",)),
        dst.Destination("Egypt", "Egypt-Mobile (Etisalat)", ()),  # empty
        dst.Destination("Albania", "Albania", ()),                # empty
        dst.Destination("Albania", "Albania-Tirana", ("355441",)),
    ]


def test_countries_sorted_unique() -> None:
    items = _sample_items()
    assert dst.countries(items) == ["Albania", "Egypt"]


def test_zones_for_returns_all_in_country() -> None:
    items = _sample_items()
    egypt = dst.zones_for(items, "Egypt")
    assert egypt == sorted(
        ["Egypt", "Egypt-Mobile (Vodafone)", "Egypt-Mobile (Etisalat)"]
    )
    # Includes the empty-numbers entry.
    assert "Egypt-Mobile (Etisalat)" in egypt


def test_zones_for_unknown_country_empty() -> None:
    assert dst.zones_for(_sample_items(), "Nowhere") == []


def test_zones_with_numbers_filters_empty() -> None:
    items = _sample_items()
    assert dst.zones_with_numbers(items, "Egypt") == sorted(
        ["Egypt", "Egypt-Mobile (Vodafone)"]
    )
    assert dst.zones_with_numbers(items, "Albania") == ["Albania-Tirana"]


def test_lookup_finds_populated_and_empty_rows() -> None:
    items = _sample_items()
    populated = dst.lookup(items, "Egypt", "Egypt")
    assert populated is not None and populated.numbers == ("20212345678",)
    empty = dst.lookup(items, "Egypt", "Egypt-Mobile (Etisalat)")
    assert empty is not None and empty.numbers == ()
    assert dst.lookup(items, "Egypt", "Nope") is None
    assert dst.lookup(items, "Nowhere", "Egypt") is None


def test_any_zone_has_numbers() -> None:
    seed = [dst.Destination("Egypt", "Egypt", ())]
    assert dst.any_zone_has_numbers(seed) is False
    seed.append(dst.Destination("Albania", "Albania", ("355441",)))
    assert dst.any_zone_has_numbers(seed) is True


def test_any_zone_has_numbers_on_bundled_seed(
    isolated_user_dir: Path,
) -> None:
    """The shipped seed catalogue has all-empty numbers -> False.
    This is the trigger that hides the Test Runner row until an
    operator fills at least one number in via Settings."""
    items = dst.load_destinations()
    assert dst.any_zone_has_numbers(items) is False


# ----------------------------------------------------------------------
# save_destinations round-trip
# ----------------------------------------------------------------------
def test_save_round_trips_atomically(isolated_user_dir: Path) -> None:
    items = [
        dst.Destination("Egypt", "Egypt", ("20212345678",)),
        dst.Destination("Egypt", "Egypt-Mobile (Vodafone)", ("201001234567", "201001234568")),
        dst.Destination("Albania", "Albania", ()),
    ]
    dst.save_destinations(items)
    user_path = isolated_user_dir / "destinations.json"
    assert user_path.exists()
    # No stray .tmp left after a successful atomic replace.
    assert not (isolated_user_dir / "destinations.json.tmp").exists()
    reloaded = dst.load_destinations()
    assert reloaded == items


def test_save_then_modify_then_save(isolated_user_dir: Path) -> None:
    items = [dst.Destination("Egypt", "Egypt", ("111",))]
    dst.save_destinations(items)
    items2 = [
        dst.Destination("Egypt", "Egypt", ("111",)),
        dst.Destination("Albania", "Albania-Tirana", ("355441",)),
    ]
    dst.save_destinations(items2)
    assert dst.load_destinations() == items2
