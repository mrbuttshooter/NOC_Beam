"""Headset HID detection (foundation).

Uses the optional `hid` (hidapi) package to enumerate connected USB HID
devices and log any that match well-known headset vendors. The full
button-event hookup (answer / hangup / mute / volume) requires per-vendor
SDKs — Jabra Xpress, Poly Lens, EPOS Connect — which are large
integrations; this module just gives us the detection surface so the
settings panel can show "Jabra Evolve2 75 detected" today and we can
wire actual call-control events in a later phase.

If hidapi isn't installed, the controller reports an empty list and the
caller silently degrades.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Vendor IDs (decimal) for the major call-control headset makers.
# Keep this list curated; if a device is missing the user can still use
# its audio path, they just won't get the badge in the UI.
HEADSET_VENDORS = {
    0x0B0E: "GN Audio (Jabra)",
    0x047F: "Plantronics / Poly",
    0x1395: "Sennheiser / EPOS",
    0x046D: "Logitech",
    0x05A7: "Bose",
    0x1532: "Razer",
    0x041E: "Creative",
}


@dataclass(frozen=True)
class HeadsetInfo:
    vendor_id: int
    product_id: int
    vendor_name: str
    product_name: str

    def __str__(self) -> str:
        return f"{self.vendor_name} — {self.product_name or 'HID'}"


def detect_headsets() -> list[HeadsetInfo]:
    """Best-effort scan. Empty list if hidapi is missing or nothing matches."""
    try:
        import hid  # type: ignore
    except Exception:
        log.debug("hidapi not available; headset detection disabled")
        return []

    found: list[HeadsetInfo] = []
    try:
        for entry in hid.enumerate():
            vid = entry.get("vendor_id", 0)
            if vid not in HEADSET_VENDORS:
                continue
            product = entry.get("product_string") or ""
            found.append(HeadsetInfo(
                vendor_id=vid,
                product_id=entry.get("product_id", 0),
                vendor_name=HEADSET_VENDORS[vid],
                product_name=product.strip(),
            ))
    except Exception:
        log.exception("hidapi enumerate raised")
        return []

    # Deduplicate (same physical headset enumerates multiple HID usage pages).
    seen = set()
    unique: list[HeadsetInfo] = []
    for h in found:
        key = (h.vendor_id, h.product_id, h.product_name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(h)
    return unique
