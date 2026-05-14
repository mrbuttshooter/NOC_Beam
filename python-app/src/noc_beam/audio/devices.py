"""Audio device enumeration via pjsua2's AudDevManager."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioDevice:
    index: int          # pjsua2 device id; -1 means "system default"
    name: str
    driver: str
    input_channels: int
    output_channels: int

    @property
    def is_input(self) -> bool:
        return self.input_channels > 0

    @property
    def is_output(self) -> bool:
        return self.output_channels > 0


def enumerate_devices() -> list[AudioDevice]:
    if not PJSUA2_AVAILABLE:
        return []
    try:
        ep = pj.Endpoint.instance()
    except Exception:
        return []
    try:
        adm = ep.audDevManager()
        infos = adm.enumDev2()
    except Exception:
        log.exception("Failed to enumerate audio devices")
        return []

    devices: list[AudioDevice] = []
    for i, info in enumerate(infos):
        devices.append(
            AudioDevice(
                index=i,
                name=info.name,
                driver=info.driver,
                input_channels=info.inputCount,
                output_channels=info.outputCount,
            )
        )
    return devices


def set_active_devices(input_idx: int, output_idx: int) -> None:
    if not PJSUA2_AVAILABLE:
        return
    try:
        ep = pj.Endpoint.instance()
        adm = ep.audDevManager()
        adm.setCaptureDev(input_idx)
        adm.setPlaybackDev(output_idx)
    except Exception:
        log.exception("Failed to set audio devices (cap=%d play=%d)", input_idx, output_idx)
