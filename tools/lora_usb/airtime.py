"""Exact SX126x LoRa time-on-air for this link's framing.

The formula is the Semtech SX1261/2 datasheet time-on-air expression (rev 2.1,
section 6.1.4), which RadioLib 7.x also implements.  It is a calculation of the
modulated packet length, not a measurement: TX ramp, TCXO start-up and
RX/TX turnaround are excluded and must be added from bench measurements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .application import CRC_SIZE as APP_CRC_SIZE
from .application import HEADER_SIZE as APP_HEADER_SIZE
from .application import RTCM_FRAGMENT_DATA_SIZE

#: Modem air header (16 bytes) plus air CRC16 (2 bytes) around each payload.
RADIO_OVERHEAD_BYTES = 18
#: Application header plus CRC16 around each application body.
APPLICATION_OVERHEAD_BYTES = APP_HEADER_SIZE + APP_CRC_SIZE
#: ``rtcm_type:u16, total_length:u16, index:u8, count:u8``.
RTCM_FRAGMENT_METADATA_BYTES = 6
#: Bytes added on air to each RTCM fragment's slice of the RTCM3 frame.
RTCM_FRAGMENT_OVERHEAD_BYTES = (
    RTCM_FRAGMENT_METADATA_BYTES + APPLICATION_OVERHEAD_BYTES + RADIO_OVERHEAD_BYTES
)
#: Telemetry body is a fixed 24-byte schema.
TELEMETRY_AIR_BYTES = 24 + APPLICATION_OVERHEAD_BYTES + RADIO_OVERHEAD_BYTES


@dataclass(frozen=True)
class LoRaPhy:
    """LoRa PHY parameters.  ``coding_rate`` is the 4/x denominator (5..8)."""

    spreading_factor: int
    bandwidth_hz: float
    coding_rate: int = 5
    preamble_symbols: int = 12
    explicit_header: bool = True
    crc: bool = True
    low_data_rate_optimize: bool | None = None

    def __post_init__(self) -> None:
        if not 5 <= self.spreading_factor <= 12:
            raise ValueError("spreading factor must be 5..12")
        if self.bandwidth_hz <= 0:
            raise ValueError("bandwidth must be positive")
        if not 5 <= self.coding_rate <= 8:
            raise ValueError("coding rate denominator must be 5..8")
        if self.preamble_symbols < 1:
            raise ValueError("preamble must be at least one symbol")

    @property
    def symbol_s(self) -> float:
        return (1 << self.spreading_factor) / self.bandwidth_hz

    @property
    def ldro(self) -> bool:
        # RadioLib enables LDRO automatically for symbols of 16 ms or longer.
        if self.low_data_rate_optimize is not None:
            return self.low_data_rate_optimize
        return self.symbol_s >= 0.016

    def symbols(self, payload_bytes: int) -> float:
        if payload_bytes < 0:
            raise ValueError("payload length must be non-negative")
        sf = self.spreading_factor
        crc_bits = 16 if self.crc else 0
        header_bits = 20 if self.explicit_header else 0
        cr = self.coding_rate - 4
        if sf <= 6:
            fixed = self.preamble_symbols + 6.25 + 8
            numerator = 8 * payload_bytes + crc_bits - 4 * sf + header_bits
            denominator = 4 * sf
        else:
            fixed = self.preamble_symbols + 4.25 + 8
            numerator = 8 * payload_bytes + crc_bits - 4 * sf + 8 + header_bits
            denominator = 4 * (sf - 2) if self.ldro else 4 * sf
        return fixed + math.ceil(max(numerator, 0) / denominator) * (cr + 4)

    def time_on_air_s(self, payload_bytes: int) -> float:
        return self.symbols(payload_bytes) * self.symbol_s

    def describe(self) -> str:
        return (
            f"SF{self.spreading_factor}/BW{self.bandwidth_hz / 1000:g}k/"
            f"CR4/{self.coding_rate}/pre{self.preamble_symbols}"
        )


#: Profile compiled into ``firmware/lora_radio/src/main.cpp``.
FIRMWARE_PHY = LoRaPhy(spreading_factor=5, bandwidth_hz=500_000)
#: Profile used before PR #2; earlier recorded airtime estimates used it.
LEGACY_BW250_PHY = LoRaPhy(spreading_factor=5, bandwidth_hz=250_000)


def application_airtime_s(application_bytes: int, phy: LoRaPhy = FIRMWARE_PHY) -> float:
    """Airtime of one encoded application frame carried by the modem."""
    return phy.time_on_air_s(application_bytes + RADIO_OVERHEAD_BYTES)


def rtcm_fragment_sizes(frame_bytes: int) -> list[int]:
    """On-air packet sizes produced by ``application.fragment`` for one frame."""
    if frame_bytes <= 0:
        raise ValueError("RTCM frame length must be positive")
    full, tail = divmod(frame_bytes, RTCM_FRAGMENT_DATA_SIZE)
    data = [RTCM_FRAGMENT_DATA_SIZE] * full + ([tail] if tail else [])
    return [part + RTCM_FRAGMENT_OVERHEAD_BYTES for part in data]


def rtcm_frame_airtime_s(frame_bytes: int, phy: LoRaPhy = FIRMWARE_PHY) -> float:
    return sum(phy.time_on_air_s(size) for size in rtcm_fragment_sizes(frame_bytes))
