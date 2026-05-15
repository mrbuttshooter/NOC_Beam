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
from dataclasses import asdict, dataclass, field
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

    def to_storable(self) -> dict[str, Any]:
        d = asdict(self)
        d["password"] = _protect(self.password)
        return d

    @classmethod
    def from_storable(cls, d: dict[str, Any]) -> "AccountConfig":
        pw = _unprotect(d.get("password", ""))
        return cls(**{**d, "password": pw})


@dataclass
class AudioSettings:
    input_device: int = -1          # -1 = system default
    output_device: int = -1
    ringer_device: int = -1
    ec_tail_ms: int = 200
    clock_rate: int = 16000


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
class GlobalSettings:
    audio: AudioSettings = field(default_factory=AudioSettings)
    codecs: CodecSettings = field(default_factory=CodecSettings)
    appearance: AppearanceSettings = field(default_factory=AppearanceSettings)
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
        audio = AudioSettings(**raw.get("audio", {}))
        codecs = CodecSettings(**raw.get("codecs", {}))
        appearance = AppearanceSettings(**raw.get("appearance", {}))
        return GlobalSettings(
            audio=audio,
            codecs=codecs,
            appearance=appearance,
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
