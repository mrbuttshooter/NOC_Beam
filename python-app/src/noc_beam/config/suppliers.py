"""Shared carrier / supplier list.

One flat list of (id, name) entries shared across all accounts. Each
account applies its own routing format (e.g. "U{id}" for Teles UK,
"000{id}" for Genband) to derive the actual auth username or dial
prefix at call time.

Storage:
- Bundled default: src/noc_beam/data/suppliers.default.json
- Per-user copy:    %APPDATA%/NOC_Beam/suppliers.json
- On first run we copy the default to the per-user path; thereafter
  the per-user copy is the source of truth, edited via Settings.

Format (preserved verbatim, including any leading zeros):
    [{"id": "080", "name": "Ibasis (Premium)"}, ...]
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from noc_beam.config.paths import data_dir

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Supplier:
    """A carrier entry shared across accounts.

    `id` is a string (preserves leading zeros like "005") because the
    routing format substitutes it as-is: format="U{id}", id="005"
    -> "U005". Storing as int would silently break that.

    `valid` toggles whether the supplier shows up in the picker. Older
    saved JSON files without this field default to True.
    """
    id: str
    name: str
    valid: bool = True

    def display(self) -> str:
        """How the picker shows it: 'Telecom Egypt — C303'.

        Carrier IDs are conventionally written with a 'C' prefix in
        the user's NOC workflow ("C303", "C080") even though the
        underlying id stored on disk is just the digits. The 'C' is
        a display affordance; routing formats still substitute the
        raw id (e.g. U{id} -> "U303", not "UC303").
        """
        return f"{self.name} — C{self.id}"

    def routed(self, format_template: str) -> str:
        """Apply an account's routing format. format_template uses
        {id} as the placeholder. Examples:
            id="303", format="U{id}"   -> "U303"
            id="303", format="000{id}" -> "000303"
            id="005", format="N{id}"   -> "N005"
        If the template doesn't contain {id} we return it verbatim --
        useful for "this account has a fixed username regardless of
        supplier" edge cases.
        """
        if not format_template:
            return self.id
        try:
            return format_template.format(id=self.id)
        except (KeyError, IndexError, ValueError):
            log.warning("Bad routing format %r for supplier %s", format_template, self.id)
            return self.id


# ---------------------------------------------------------------------------
# Defaults (bundled). Editable copy lives in %APPDATA%.
# ---------------------------------------------------------------------------
DEFAULT_SUPPLIERS: list[Supplier] = [
    Supplier(id="080", name="Ibasis (Premium)"),
    Supplier(id="303", name="Telecom Egypt"),
    Supplier(id="005", name="Etisalat (UAE)"),
    Supplier(id="138", name="MCI Group"),
]


def _default_json_path() -> Path:
    """Bundled default file (read-only, lives inside the package)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "noc_beam" / "data" / "suppliers.default.json"
    return Path(__file__).parent.parent / "data" / "suppliers.default.json"


def _user_json_path() -> Path:
    """Per-user editable copy."""
    return data_dir() / "suppliers.json"


def load_suppliers() -> list[Supplier]:
    """Load suppliers, falling back through:
        1. %APPDATA%/NOC_Beam/suppliers.json (user-edited)
        2. Bundled suppliers.default.json (read-only)
        3. Hard-coded DEFAULT_SUPPLIERS (last resort)
    On first run, step 2's contents are copied into step 1's path so
    subsequent saves persist.
    """
    user_path = _user_json_path()
    if user_path.exists():
        try:
            raw = json.loads(user_path.read_text(encoding="utf-8"))
            return _parse(raw)
        except Exception:
            log.exception("Failed to load %s; falling back to bundled defaults", user_path)

    default_path = _default_json_path()
    if default_path.exists():
        try:
            raw = json.loads(default_path.read_text(encoding="utf-8"))
            suppliers = _parse(raw)
            # Seed the per-user copy so the Settings editor has somewhere
            # to write to and the bundled file stays read-only.
            try:
                save_suppliers(suppliers)
            except Exception:
                log.exception("Could not seed user suppliers.json from bundled default")
            return suppliers
        except Exception:
            log.exception("Failed to load bundled %s; using hard-coded defaults", default_path)

    return list(DEFAULT_SUPPLIERS)


def save_suppliers(suppliers: list[Supplier]) -> None:
    """Persist the supplier list to the per-user JSON. Atomic write."""
    path = _user_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(s) for s in suppliers]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        tmp.replace(path)
    except PermissionError:
        log.warning("Atomic replace failed for %s; falling back to direct write", path)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _parse(raw: list) -> list[Supplier]:
    out: list[Supplier] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id")
        name = entry.get("name", "")
        if sid is None:
            continue
        # `valid` defaults to True for older files that don't carry it.
        valid = bool(entry.get("valid", True))
        # Preserve string form; ints get coerced to str so leading-zero
        # IDs that round-trip through other tooling stay intact.
        out.append(Supplier(id=str(sid), name=str(name), valid=valid))
    return out


def load_valid_suppliers() -> list[Supplier]:
    """Same as `load_suppliers()` but filters out invalid ones.

    Used by the picker widgets (dialpad + Test Runner) so operators
    only see suppliers their org has actually authorised. Settings
    has the full list with a checkbox per row.
    """
    return [s for s in load_suppliers() if s.valid]
