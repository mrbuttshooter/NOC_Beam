"""Persistent settings, accounts, and password storage.

Account passwords are encrypted with Windows DPAPI (CurrentUser scope) when
available; on other platforms they are stored as base64 so they survive a
roundtrip but are not 'protected' — clearly logged at startup.
"""
from __future__ import annotations

import base64
import json
import logging
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from noc_beam.config.paths import accounts_file, settings_file

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DPAPI password protection (Windows only)
# ---------------------------------------------------------------------------
def _protect(plaintext: str) -> str:
    if not plaintext:
        return ""
    if sys.platform == "win32":
        try:
            import win32crypt  # type: ignore

            blob = win32crypt.CryptProtectData(
                plaintext.encode("utf-8"), "NOC_Beam", None, None, None, 0
            )
            return "dpapi:" + base64.b64encode(blob).decode("ascii")
        except Exception:  # pragma: no cover - falls through to base64
            log.warning("DPAPI protection failed, falling back to base64", exc_info=True)
    return "b64:" + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")


def _unprotect(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith("dpapi:") and sys.platform == "win32":
        try:
            import win32crypt  # type: ignore

            blob = base64.b64decode(stored[len("dpapi:") :])
            _desc, plaintext = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
            return plaintext.decode("utf-8")
        except Exception:  # pragma: no cover
            log.warning("DPAPI unprotect failed", exc_info=True)
            return ""
    if stored.startswith("b64:"):
        try:
            return base64.b64decode(stored[len("b64:") :]).decode("utf-8")
        except Exception:
            return ""
    return stored  # legacy plaintext


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class AccountConfig:
    id: str
    display_name: str = ""
    username: str = ""
    auth_user: str = ""
    domain: str = ""
    password: str = ""              # plaintext in-memory only
    proxy: str = ""
    transport: str = "udp"          # udp | tcp | tls
    register: bool = True
    srtp: str = "disabled"          # disabled | optional | mandatory
    dtmf_method: str = "rfc2833"    # rfc2833 | info | inband
    stun_server: str = ""
    enabled: bool = True
    # Optional non-default port. 0 / blank = use the transport default
    # (UDP/TCP 5060, TLS 5061). When set, overrides PJSIP's resolution.
    port: int = 0

    def to_storable(self) -> dict[str, Any]:
        d = asdict(self)
        d["password"] = _protect(self.password)
        return d

    @classmethod
    def from_storable(cls, d: dict[str, Any]) -> "AccountConfig":
        # Filter to known fields so an unknown key (forward-compat) or a
        # removed field doesn't crash with TypeError -- without this
        # one bad row in accounts.json silently wiped EVERY account.
        from dataclasses import fields as _fields
        known = {f.name for f in _fields(cls)}
        clean = {k: v for k, v in d.items() if k in known}
        clean["password"] = _unprotect(d.get("password", ""))
        # Backfill required `id` if missing (corrupted file recovery).
        if "id" not in clean or not clean["id"]:
            import uuid as _uuid
            clean["id"] = str(_uuid.uuid4())
        return cls(**clean)


@dataclass
class AudioSettings:
    input_device: int = -1          # -1 = system default
    output_device: int = -1
    ringer_device: int = -1
    ec_tail_ms: int = 200
    clock_rate: int = 16000
    # Persisted volumes 0..100 -- the AudioStrip top-bar sliders
    # write these on every adjust. Without declaring them on the
    # dataclass, the writer succeeds at runtime (Python allows
    # arbitrary attribute creation) but `asdict()` in save_settings
    # only emits declared fields, so the value gets silently
    # dropped on disk and the sliders reset to 75 on every restart.
    master_volume_pct: int = 75
    mic_volume_pct: int = 75


@dataclass
class CodecSettings:
    # Map of codec identifier substring -> priority (0..255, 0=disabled).
    # PJSIP codec ids look like "PCMU/8000/1", "G729/8000/1", "opus/48000/2", etc.
    priorities: dict[str, int] = field(default_factory=lambda: {
        "PCMA/8000": 245,
        "PCMU/8000": 240,
        "G722/16000": 235,
        "opus/48000": 230,
        "G729/8000": 220,
        "iLBC/8000": 210,
        "speex/16000": 200,
        "speex/8000": 195,
        "GSM/8000": 190,
    })


@dataclass
class AppearanceSettings:
    # Honoured by the trace-drawer slide and the rail's LIVE pulse.
    # Maps to the design system's prefers-reduced-motion gate.
    reduced_motion: bool = False
    # Swap dark.qss <-> dark-hc.qss. Phase F wires the toggle.
    high_contrast: bool = False
    # Theme: "light" (Bria-evolution default) | "dark" (NOC dashboard look).
    theme: str = "light"


@dataclass
class StartupSettings:
    # Three boxes from Settings -> General -> Startup card.
    # `start_with_windows` is wired up at launcher level (registry
    # Run key); the other two are read by main.py at boot.
    start_with_windows: bool = False
    start_minimized: bool = False
    restore_window_pos: bool = True


@dataclass
class ComplianceSettings:
    """Compliance + privacy preferences.

    All default to the SAFER setting -- redact PII in traces, require
    consent before recording, etc. Users / IT can flip toggles per
    jurisdiction, but the out-of-box experience is GDPR + EU-Accessibility
    -Act + two-party-consent friendly.
    """
    # Call recording is gated behind explicit consent. When True, the
    # UI shows a "Record" toggle on the active-call card and a banner
    # while recording is live. When False (default), the toggle is
    # hidden entirely so a misclick can't begin recording.
    call_recording_enabled: bool = False
    # When True, recording starts ONLY after the user accepts a
    # per-call consent dialog. The dialog reminds the user that
    # most jurisdictions require notifying the remote party.
    recording_consent_required: bool = True
    # When True, the in-app SIP trace masks Authorization/digest
    # headers and SIP URI user-parts before display + export. When
    # False, full wire content is captured (diagnostic mode -- the
    # UI shows a banner so the user knows raw capture is on).
    trace_pii_redaction: bool = True


@dataclass
class GlobalSettings:
    audio: AudioSettings = field(default_factory=AudioSettings)
    codecs: CodecSettings = field(default_factory=CodecSettings)
    appearance: AppearanceSettings = field(default_factory=AppearanceSettings)
    startup: StartupSettings = field(default_factory=StartupSettings)
    compliance: ComplianceSettings = field(default_factory=ComplianceSettings)
    sip_port: int = 0               # 0 = ephemeral
    log_level: int = 4              # PJSIP log level 0..6
    user_agent: str = "NOC_Beam/0.1"
    theme: str = "dark"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load_settings() -> GlobalSettings:
    path = settings_file()
    if not path.exists():
        return GlobalSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        # Same protection History uses: quarantine the corrupt file so
        # the next save_settings can't overwrite it with defaults and
        # silently lose every user pref. Then start fresh.
        try:
            import time as _time
            backup = path.with_name(
                f"{path.stem}.corrupted-{int(_time.time())}{path.suffix}"
            )
            path.rename(backup)
            log.error(
                "settings.json was unreadable; quarantined to %s; "
                "starting with defaults",
                backup.name,
            )
        except Exception:
            log.exception(
                "Failed to read settings AND failed to quarantine; "
                "leaving file in place"
            )
        return GlobalSettings()

    def _filter(cls, data):
        # Drop unknown keys per-section so a forward-compat field or a
        # stale dump from another build doesn't TypeError out the whole
        # load and silently wipe every setting back to defaults (the
        # bug the History audit caught for CDR rows -- same shape).
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        try:
            return cls(**clean)
        except Exception:
            log.warning("Settings section %s could not be parsed; using defaults", cls.__name__)
            return cls()

    try:
        audio = _filter(AudioSettings, raw.get("audio", {}))
        codecs = _filter(CodecSettings, raw.get("codecs", {}))
        appearance = _filter(AppearanceSettings, raw.get("appearance", {}))
        # New sections (added in audit rounds 11 + ship-blockers).
        # Without explicit unmarshal here, load_settings silently
        # returned the dataclass defaults -- making the Startup
        # checkboxes + Compliance toggles look functional in the UI
        # but resetting every launch.
        startup = _filter(StartupSettings, raw.get("startup", {}))
        compliance = _filter(ComplianceSettings, raw.get("compliance", {}))
        return GlobalSettings(
            audio=audio,
            codecs=codecs,
            appearance=appearance,
            startup=startup,
            compliance=compliance,
            sip_port=raw.get("sip_port", 0),
            log_level=raw.get("log_level", 4),
            user_agent=raw.get("user_agent", "NOC_Beam/0.1"),
            theme=raw.get("theme", "dark"),
        )
    except Exception:
        log.exception("Failed to load settings, using defaults")
        return GlobalSettings()


def save_settings(settings: GlobalSettings) -> None:
    path = settings_file()
    payload = {
        "audio": asdict(settings.audio),
        "codecs": asdict(settings.codecs),
        "appearance": asdict(settings.appearance),
        # Persist startup + compliance so toggles in Settings actually
        # round-trip across launches. Pre-fix, these were silently
        # dropped on save; UI looked functional, behavior was nil.
        "startup": asdict(settings.startup),
        "compliance": asdict(settings.compliance),
        "sip_port": settings.sip_port,
        "log_level": settings.log_level,
        "user_agent": settings.user_agent,
        "theme": settings.theme,
    }
    _atomic_write(path, json.dumps(payload, indent=2))


def load_accounts() -> list[AccountConfig]:
    path = accounts_file()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return [AccountConfig.from_storable(item) for item in raw]
    except Exception:
        log.exception("Failed to load accounts")
        return []


def save_accounts(accounts: list[AccountConfig]) -> None:
    path = accounts_file()
    payload = [a.to_storable() for a in accounts]
    _atomic_write(path, json.dumps(payload, indent=2))


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(path)
    except PermissionError:
        log.warning(
            "Atomic replace failed for %s; falling back to direct write",
            path,
            exc_info=True,
        )
        path.write_text(content, encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            log.debug("Could not remove temporary config file %s", tmp, exc_info=True)
