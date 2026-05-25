"""Saved destinations / known-good test numbers library.

A per-(country, sale-zone) catalogue of carrier test numbers the
Test Runner loads with one click. Independent of the supplier
picker -- destinations live on their own axis.

Storage:
- Bundled default: src/noc_beam/data/destinations.default.json
- Per-user copy:    %APPDATA%/NOC_Beam/destinations.json
- On first run we seed the per-user copy from the bundled default;
  thereafter the per-user copy is the source of truth, edited via
  Settings -> Destinations.

JSON schema (forward-compatible -- v1 ships with empty number lists
for all 1,400 seeded zones; operators populate them via Settings as
they verify):
    [
      {"country": "Egypt", "zone": "Egypt-Mobile (Vodafone)",
       "numbers": ["201001234567"]},
      ...
    ]

`(country, zone)` is the logical key. Last duplicate wins (with a
warning logged). Rows with empty `numbers` are intentionally KEPT
in memory -- the seed file is exactly that and Settings is the
fill-in surface. Test Runner gates on
`zones_with_numbers(items, country)` to hide unfillable zones from
the picker while Settings shows all of them via `zones_for`.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from noc_beam.config.paths import data_dir

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Destination:
    """A single saved (country, zone, numbers) entry.

    `zone` is the verbatim Sale Codes label, e.g. "Egypt-Mobile (Vodafone)".
    Keeping the country prefix in the zone label makes each row
    self-identifying and matches Sale Codes 1:1 with no normalisation
    step that could drift.

    `numbers` is a tuple of E.164-style digit strings (no `+`, no
    `sip:` prefix -- matches what operators paste into Targets today).
    Empty tuple is valid: the seed catalogue ships with empty number
    lists everywhere and operators fill them in via Settings.
    """
    country: str
    zone: str
    numbers: tuple[str, ...]


def country_of(zone_label: str) -> str:
    """Extract the country display name from a Sale Codes zone label.

    Rule: substring before the first '-', or the whole label if no
    '-'. Whitespace is stripped from both sides.

    Examples:
        "Egypt"                          -> "Egypt"
        "Egypt-Mobile (Vodafone)"        -> "Egypt"
        "Egypt-Fix-Special Services"     -> "Egypt"
        "Aeromobile"                     -> "Aeromobile"
        " Angola "                       -> "Angola"
    """
    label = (zone_label or "").strip()
    if not label:
        return ""
    head = label.split("-", 1)[0]
    return head.strip()


def _default_json_path() -> Path:
    """Bundled default file (read-only, lives inside the package)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "noc_beam" / "data" / "destinations.default.json"
    return Path(__file__).parent.parent / "data" / "destinations.default.json"


def _user_json_path() -> Path:
    """Per-user editable copy."""
    return data_dir() / "destinations.json"


def load_destinations() -> list[Destination]:
    """Load destinations, falling back through:
        1. %APPDATA%/NOC_Beam/destinations.json (user-edited)
        2. Bundled destinations.default.json (read-only)
        3. Empty list (last resort)
    On first run, step 2's contents are copied to step 1's path so
    subsequent saves persist.
    """
    user_path = _user_json_path()
    if user_path.exists():
        try:
            raw = json.loads(user_path.read_text(encoding="utf-8"))
            return _parse(raw)
        except Exception:
            log.exception(
                "Failed to load %s; falling back to bundled defaults", user_path
            )

    default_path = _default_json_path()
    if default_path.exists():
        try:
            raw = json.loads(default_path.read_text(encoding="utf-8"))
            items = _parse(raw)
            # Seed the per-user copy so the Settings editor has somewhere
            # to write to and the bundled file stays read-only.
            try:
                save_destinations(items)
            except Exception:
                log.exception(
                    "Could not seed user destinations.json from bundled default"
                )
            return items
        except Exception:
            log.exception(
                "Failed to load bundled %s; returning empty list", default_path
            )

    return []


def save_destinations(items: list[Destination]) -> None:
    """Persist the destinations list to the per-user JSON. Atomic write
    (`.tmp` + `os.replace`) with a fallback to direct write on
    PermissionError -- matches the supplier save pattern.
    """
    path = _user_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "country": d.country,
            "zone": d.zone,
            "numbers": list(d.numbers),
        }
        for d in items
    ]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        tmp.replace(path)
    except PermissionError:
        log.warning("Atomic replace failed for %s; falling back to direct write", path)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _parse(raw: list) -> list[Destination]:
    """Parse raw JSON list into Destination entries.

    Validation rules:
    - Rows that aren't a dict, or are missing `country` / `zone`, are
      skipped with a warning.
    - Empty `numbers` is ACCEPTED (the seed catalogue is exactly that).
    - Duplicate `(country, zone)` keys -> last wins, with a warning.
    - Number entries are coerced to str and stripped; falsy entries
      are dropped silently (preserves intent without ditching the row).
    """
    if not isinstance(raw, list):
        log.warning("destinations JSON root is not a list; ignoring")
        return []
    by_key: dict[tuple[str, str], Destination] = {}
    order: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning("Skipping non-dict destinations row: %r", entry)
            continue
        country = entry.get("country")
        zone = entry.get("zone")
        if not country or not zone:
            log.warning(
                "Skipping destinations row missing country/zone: %r", entry
            )
            continue
        country = str(country).strip()
        zone = str(zone).strip()
        if not country or not zone:
            log.warning(
                "Skipping destinations row with empty country/zone: %r", entry
            )
            continue
        raw_numbers = entry.get("numbers") or []
        if not isinstance(raw_numbers, list):
            log.warning(
                "Destinations row numbers not a list (using empty): %r", entry
            )
            raw_numbers = []
        numbers = tuple(
            n for n in (str(x).strip() for x in raw_numbers) if n
        )
        key = (country, zone)
        if key in by_key:
            log.warning(
                "Duplicate destinations row (country=%r, zone=%r) -- last wins",
                country,
                zone,
            )
        else:
            order.append(key)
        by_key[key] = Destination(country=country, zone=zone, numbers=numbers)
    return [by_key[k] for k in order]


def countries(items: list[Destination]) -> list[str]:
    """Sorted unique list of countries across all items."""
    return sorted({d.country for d in items})


def zones_for(items: list[Destination], country: str) -> list[str]:
    """All zones in a country, sorted alphabetically. Includes zones
    whose `numbers` is empty -- used by the Settings editor."""
    target = (country or "").strip()
    return sorted({d.zone for d in items if d.country == target})


def zones_with_numbers(items: list[Destination], country: str) -> list[str]:
    """Zones in a country that have at least one number, sorted.
    Used by the Test Runner picker so operators never pick a zone
    with nothing to load.
    """
    target = (country or "").strip()
    return sorted({d.zone for d in items if d.country == target and d.numbers})


def lookup(
    items: list[Destination], country: str, zone: str
) -> Destination | None:
    """Find the row for (country, zone). Returns None if not present.
    Returns the row regardless of whether `numbers` is empty -- the
    Settings editor needs the row object to populate.
    """
    target_c = (country or "").strip()
    target_z = (zone or "").strip()
    for d in items:
        if d.country == target_c and d.zone == target_z:
            return d
    return None


def any_zone_has_numbers(items: list[Destination]) -> bool:
    """True if at least one zone across the entire catalogue has any
    numbers populated. Test Runner uses this to decide whether to
    show the DESTINATION / ORIGINATION rows at all (when the seed is
    fully empty, the rows would be useless).
    """
    return any(bool(d.numbers) for d in items)
