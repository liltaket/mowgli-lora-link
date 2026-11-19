"""Calculate RF airtime, e.r.p. and band fit for candidate operating plans.

This is arithmetic, not a measurement and not a compliance verdict.  Band data
are transcribed from the Swedish PTSFS 2025:1 sections 135-149 (which implement
Decision 2006/771/EC as amended by (EU) 2025/105) and the matching rows K-Q of
ETSI EN 300 220-2 V3.3.1 Table 4.  Occupied bandwidth, e.r.p., spurious and
band-edge emissions, frequency stability and receiver parameters still need
measurement on the final hardware.  See docs/RF_COMPLIANCE_ASSESSMENT.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .airtime import (
    FIRMWARE_PHY,
    TELEMETRY_AIR_BYTES,
    LoRaPhy,
    rtcm_fragment_sizes,
)

DIPOLE_GAIN_DBI = 2.15
HOUR_S = 3600.0
#: Planning ceiling for one half-duplex channel shared by base and robot,
#: leaving room for turnaround, host latency and occasional retries.
PRACTICAL_CHANNEL_OCCUPANCY = 0.60


@dataclass(frozen=True)
class Band:
    key: str
    low_hz: float
    high_hz: float
    max_erp_mw: float
    #: Duty-cycle limit as a fraction, or ``None`` for "no requirement".
    duty_cycle: float | None
    polite_access_alternative: bool
    max_occupied_bandwidth_hz: float
    source: str


#: Non-specific SRD bands near 868 MHz that a Swedish user may use licence-free.
#: Alarm, RFID, network-access-point and wideband-data rows are excluded.
BANDS_SE: tuple[Band, ...] = (
    Band("K", 863.0e6, 865.0e6, 25, 0.001, True, 2.0e6, "PTSFS 2025:1 §135"),
    Band("L", 865.0e6, 868.0e6, 25, 0.01, True, 3.0e6, "PTSFS 2025:1 §137"),
    Band("M", 868.0e6, 868.6e6, 25, 0.01, True, 600e3, "PTSFS 2025:1 §141"),
    Band("N", 868.7e6, 869.2e6, 25, 0.001, True, 500e3, "PTSFS 2025:1 §143"),
    Band("O", 869.4e6, 869.65e6, 500, 0.10, True, 250e3, "PTSFS 2025:1 §146"),
    Band("P", 869.7e6, 870.0e6, 5, None, False, 300e3, "PTSFS 2025:1 §148"),
    Band("Q", 869.7e6, 870.0e6, 25, 0.01, True, 300e3, "PTSFS 2025:1 §149"),
)

#: EN 300 220-2 V3.3.1 Table 18, used only instead of the duty-cycle limit.
POLITE_ACCESS = {
    "min_cca_s": 160e-6,
    "max_single_transmission_s": 1.0,
    "max_dialogue_s": 4.0,
    "max_cumulative_s_per_hour_per_200khz": 100.0,
    "min_off_time_same_frequency_s": 0.100,
}


def dbm_to_mw(dbm: float) -> float:
    return 10 ** (dbm / 10)


def mw_to_dbm(mw: float) -> float:
    return 10 * math.log10(mw)


def erp_dbm(
    conducted_dbm: float, antenna_gain_dbi: float, loss_db: float = 0.0
) -> float:
    """e.r.p. is referenced to a half-wave dipole (0 dBd = 2.15 dBi)."""
    return conducted_dbm + antenna_gain_dbi - DIPOLE_GAIN_DBI - loss_db


def max_conducted_dbm(
    band: Band, antenna_gain_dbi: float, loss_db: float = 0.0
) -> float:
    return mw_to_dbm(band.max_erp_mw) - antenna_gain_dbi + DIPOLE_GAIN_DBI + loss_db


def cca_threshold_dbm(operating_channel_hz: float, erp_mw: float) -> float:
    """EN 300 220-2 Table 11 sensitivity limit plus the Table 17 CCA offset."""
    sensitivity = 10 * math.log10(operating_channel_hz / 1000) - 117
    return sensitivity + (15 if erp_mw < 100 else 11)


@dataclass(frozen=True)
class Emission:
    center_hz: float
    phy: LoRaPhy
    conducted_dbm: float
    #: Assumed 0 dBd antenna; replace with the measured antenna/cable figures.
    antenna_gain_dbi: float = DIPOLE_GAIN_DBI
    loss_db: float = 0.0
    #: 99 % occupied bandwidth as a multiple of the LoRa bandwidth (assumed).
    obw_factor: float = 1.0
    #: Crystal/TCXO frequency tolerance used for the edge margin (assumed).
    tolerance_ppm: float = 10.0

    @property
    def erp_dbm(self) -> float:
        return erp_dbm(self.conducted_dbm, self.antenna_gain_dbi, self.loss_db)

    @property
    def erp_mw(self) -> float:
        return dbm_to_mw(self.erp_dbm)

    @property
    def occupied_hz(self) -> float:
        return self.phy.bandwidth_hz * self.obw_factor

    def edges_hz(self) -> tuple[float, float]:
        drift = self.center_hz * self.tolerance_ppm * 1e-6
        half = self.occupied_hz / 2
        return self.center_hz - half - drift, self.center_hz + half + drift


def bands_for(emission: Emission, bands: tuple[Band, ...] = BANDS_SE) -> list[Band]:
    low, high = emission.edges_hz()
    return [
        band
        for band in bands
        if band.low_hz <= low
        and high <= band.high_hz
        and emission.occupied_hz <= band.max_occupied_bandwidth_hz
    ]


# --------------------------------------------------------------------------
# RTCM3 size model
# --------------------------------------------------------------------------

#: Per-satellite and per-cell bit counts from RTCM 10403.3 MSM definitions.
_MSM_BITS = {4: (18, 48), 5: (36, 63), 6: (18, 65), 7: (36, 80)}
_MSM_HEADER_BITS = 169
_CONSTELLATION_BASE = {"GPS": 1070, "GLONASS": 1080, "Galileo": 1090, "SBAS": 1100}
_CONSTELLATION_BASE |= {"QZSS": 1110, "BeiDou": 1120}

#: Complete RTCM3 frame sizes (3-byte header + payload + CRC24Q).
EPHEMERIS_FRAME_BYTES = {1019: 67, 1020: 51, 1042: 70, 1044: 67, 1045: 68, 1046: 69}
STATIC_FRAME_BYTES = {1005: 25, 1006: 27, 1230: 18}


def msm_frame_bytes(level: int, satellites: int, signals: int) -> int:
    """Complete MSM frame size assuming every satellite tracks every signal."""
    if level not in _MSM_BITS:
        raise ValueError("MSM level must be 4..7")
    if satellites < 1 or signals < 1 or satellites * signals > 64:
        raise ValueError("MSM cell mask holds 1..64 cells")
    per_sat, per_cell = _MSM_BITS[level]
    cells = satellites * signals
    bits = _MSM_HEADER_BITS + cells + satellites * per_sat + cells * per_cell
    return math.ceil(bits / 8) + 6


@dataclass(frozen=True)
class RtcmMessage:
    message_type: int
    frame_bytes: int
    interval_s: float = 1.0


def msm(constellation: str, level: int, satellites: int, signals: int, interval_s=1.0):
    return RtcmMessage(
        _CONSTELLATION_BASE[constellation] + level,
        msm_frame_bytes(level, satellites, signals),
        interval_s,
    )


#: Satellites/signals chosen so the 14-type mix totals ~2,139 B/s, the rate
#: measured from the live base in docs/TEST_RESULTS.md.  The split per type is
#: illustrative; replace it with ``--rtcm-recording`` once a capture exists.
_LIVE_SKY = {
    "GPS": (11, 3),
    "GLONASS": (8, 2),
    "Galileo": (10, 3),
    "BeiDou": (15, 3),
    "SBAS": (3, 1),
    "QZSS": (1, 3),
}


def _mix(level, constellations, *, signals=None, interval_s=1.0, extra=()):
    messages = []
    for name in constellations:
        sats, sigs = _LIVE_SKY[name]
        messages.append(msm(name, level, sats, signals or sigs, interval_s))
    return tuple(messages) + tuple(extra)


_EPHEMERIS = tuple(RtcmMessage(t, n) for t, n in EPHEMERIS_FRAME_BYTES.items())
_STATIC_1S = (RtcmMessage(1005, 25), RtcmMessage(1230, 18))
_STATIC_10S = (RtcmMessage(1005, 25, 10.0), RtcmMessage(1230, 18, 10.0))
_FOUR = ("GPS", "GLONASS", "Galileo", "BeiDou")
_THREE = ("GPS", "Galileo", "BeiDou")


@dataclass(frozen=True)
class RtcmScenario:
    key: str
    description: str
    messages: tuple[RtcmMessage, ...]


RTCM_SCENARIOS: tuple[RtcmScenario, ...] = (
    RtcmScenario(
        "live",
        "observed 14-type base output: 6x MSM7 + 6 ephemerides + 1005/1230 @1 Hz",
        _mix(7, tuple(_LIVE_SKY), extra=_EPHEMERIS + _STATIC_1S),
    ),
    RtcmScenario(
        "msm7-4c",
        "MSM7 GPS/GLO/GAL/BDS @1 Hz, no ephemerides/SBAS/QZSS, 1005+1230 @0.1 Hz",
        _mix(7, _FOUR, extra=_STATIC_10S),
    ),
    RtcmScenario(
        "msm4-4c",
        "MSM4 GPS/GLO/GAL/BDS @1 Hz, 1005+1230 @0.1 Hz (Mammotion-like content)",
        _mix(4, _FOUR, extra=_STATIC_10S),
    ),
    RtcmScenario(
        "msm4-3c",
        "MSM4 GPS/GAL/BDS triple-band @1 Hz, 1005 @0.1 Hz",
        _mix(4, _THREE, extra=(RtcmMessage(1005, 25, 10.0),)),
    ),
    RtcmScenario(
        "msm4-3c-2f",
        "MSM4 GPS/GAL/BDS dual-band @1 Hz, 1005 @0.1 Hz",
        _mix(4, _THREE, signals=2, extra=(RtcmMessage(1005, 25, 10.0),)),
    ),
    RtcmScenario(
        "msm4-3c-2s",
        "MSM4 GPS/GAL/BDS triple-band every 2 s, 1005 @0.1 Hz",
        _mix(4, _THREE, interval_s=2.0, extra=(RtcmMessage(1005, 25, 10.0),)),
    ),
    RtcmScenario(
        "msm4-3c-2f-5s",
        "MSM4 GPS/GAL/BDS dual-band every 5 s, 1005 @0.05 Hz",
        _mix(
            4, _THREE, signals=2, interval_s=5.0, extra=(RtcmMessage(1005, 25, 20.0),)
        ),
    ),
)


# --------------------------------------------------------------------------
# Traffic and airtime
# --------------------------------------------------------------------------


@dataclass
class Traffic:
    """Packets per second by on-air packet size, for one transmitter."""

    packets: dict[int, float] = field(default_factory=lambda: defaultdict(float))

    def add(self, air_bytes: int, per_second: float) -> None:
        self.packets[air_bytes] += per_second

    def merged(self, other: Traffic) -> Traffic:
        out = Traffic()
        for source in (self, other):
            for size, rate in source.packets.items():
                out.add(size, rate)
        return out

    @property
    def packets_per_second(self) -> float:
        return sum(self.packets.values())

    @property
    def bytes_per_second(self) -> float:
        return sum(size * rate for size, rate in self.packets.items())

    def airtime_fraction(self, phy: LoRaPhy) -> float:
        return sum(
            rate * phy.time_on_air_s(size) for size, rate in self.packets.items()
        )

    def max_packet_s(self, phy: LoRaPhy) -> float:
        return max((phy.time_on_air_s(size) for size in self.packets), default=0.0)


def rtcm_traffic(messages) -> Traffic:
    traffic = Traffic()
    for message in messages:
        for size in rtcm_fragment_sizes(message.frame_bytes):
            traffic.add(size, 1.0 / message.interval_s)
    return traffic


def rtcm_bytes_per_second(messages) -> float:
    return sum(message.frame_bytes / message.interval_s for message in messages)


def telemetry_traffic(hz: float) -> Traffic:
    traffic = Traffic()
    if hz > 0:
        traffic.add(TELEMETRY_AIR_BYTES, hz)
    return traffic


def recording_messages(path: Path) -> tuple[list[RtcmMessage], float]:
    """Exact per-frame sizes from an ``MLR1`` recording, averaged over its span."""
    from host.mowgli_lora.rtcm_tool import _records

    records = list(_records(path))
    if len(records) < 2:
        raise ValueError("recording needs at least two timed frames")
    duration = (records[-1][0] - records[0][0]) / 1e9
    if duration <= 0:
        raise ValueError("recording has no positive duration")
    return [RtcmMessage(0, len(raw), duration) for _, raw in records], duration


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    key: str
    description: str
    emission: Emission


PLANS: tuple[Plan, ...] = (
    Plan(
        "current",
        "firmware today: 868.3 MHz SF5/BW500, +10 dBm conducted",
        Emission(868.3e6, FIRMWARE_PHY, 10),
    ),
    Plan(
        "current-lab17",
        "modem_lab_17dbm build: same PHY, +17 dBm conducted",
        Emission(868.3e6, FIRMWARE_PHY, 17),
    ),
    Plan(
        "p-bw250",
        "candidate: 869.85 MHz SF5/BW250, +4 dBm conducted (band P, 5 mW)",
        Emission(869.85e6, LoRaPhy(5, 250_000), 4),
    ),
    Plan(
        "p-bw125",
        "candidate: 869.85 MHz SF5/BW125, +4 dBm conducted (band P, 5 mW)",
        Emission(869.85e6, LoRaPhy(5, 125_000), 4),
    ),
    Plan(
        "o-bw125",
        "candidate: 869.525 MHz SF5/BW125, +14 dBm conducted (band O, 10 %)",
        Emission(869.525e6, LoRaPhy(5, 125_000), 14),
    ),
)


def assess_transmitter(emission: Emission, traffic: Traffic) -> dict:
    phy = emission.phy
    airtime = traffic.airtime_fraction(phy)
    fits = bands_for(emission)
    low, high = emission.edges_hz()
    rows = []
    for band in fits:
        erp_ok = emission.erp_mw <= band.max_erp_mw + 1e-9
        if band.duty_cycle is None:
            duty_ok, budget_s = True, None
        else:
            duty_ok = airtime <= band.duty_cycle + 1e-12
            budget_s = band.duty_cycle * HOUR_S
        rows.append(
            {
                "band": band.key,
                "source": band.source,
                "edge_margin_khz": round(
                    min(low - band.low_hz, band.high_hz - high) / 1000, 1
                ),
                "erp_limit_mw": band.max_erp_mw,
                "erp_ok": erp_ok,
                "duty_limit": band.duty_cycle,
                "duty_budget_s_per_hour": budget_s,
                "duty_ok": duty_ok,
                "duty_overrun_factor": (
                    None
                    if band.duty_cycle is None
                    else round(airtime / band.duty_cycle, 2)
                ),
                "lawful_by_duty_cycle": erp_ok and duty_ok,
            }
        )
    return {
        "airtime_fraction": round(airtime, 5),
        "airtime_s_per_hour": round(airtime * HOUR_S, 1),
        "packets_per_second": round(traffic.packets_per_second, 2),
        "longest_packet_ms": round(traffic.max_packet_s(phy) * 1000, 2),
        "erp_dbm": round(emission.erp_dbm, 2),
        "erp_mw": round(emission.erp_mw, 2),
        "bands": rows,
    }


def assess(plan: Plan, rtcm: Traffic, telemetry: Traffic) -> dict:
    base = assess_transmitter(plan.emission, rtcm)
    robot = assess_transmitter(plan.emission, telemetry)
    channel = rtcm.merged(telemetry).airtime_fraction(plan.emission.phy)
    return {
        "plan": plan.key,
        "description": plan.description,
        "phy": plan.emission.phy.describe(),
        "center_mhz": plan.emission.center_hz / 1e6,
        "base": base,
        "robot": robot,
        "shared_channel_occupancy": round(channel, 4),
        "capacity_ok": channel <= PRACTICAL_CHANNEL_OCCUPANCY,
        "lawful": bool(base["bands"])
        and any(row["lawful_by_duty_cycle"] for row in base["bands"])
        and any(row["lawful_by_duty_cycle"] for row in robot["bands"]),
    }


def max_sustained_bytes_per_second(
    phy: LoRaPhy, duty: float, frame_bytes: int
) -> float:
    """RTCM bytes/s that fit a duty fraction when every frame has ``frame_bytes``."""
    per_frame = sum(
        phy.time_on_air_s(size) for size in rtcm_fragment_sizes(frame_bytes)
    )
    return duty / per_frame * frame_bytes


# --------------------------------------------------------------------------
# Polite spectrum access (LBT + AFA) across several channels
# --------------------------------------------------------------------------

#: Bands where polite access may replace the duty cycle and that are wide
#: enough for several LoRa channels.  N is 0.1 %/500 kHz and adds little.
AFA_BANDS = ("K", "L", "M")
_PORTION_HZ = 200e3


def afa_channels(band: Band, bandwidth_hz: float) -> int:
    """Channels whose 200 kHz portions are never shared with another channel.

    Table 18 limits cumulative on-time per 200 kHz portion.  Spacing channels
    by whole portions lets each channel use the full 100 s/h without a lab
    having to decide how a partly covered portion is counted.
    """
    spacing = math.ceil(bandwidth_hz / _PORTION_HZ - 1e-9) * _PORTION_HZ
    return int((band.high_hz - band.low_hz + 1) // spacing)


def polite_access_plan(
    phy: LoRaPhy, traffic: Traffic, band_keys: tuple[str, ...] = AFA_BANDS
) -> dict:
    per_portion = POLITE_ACCESS["max_cumulative_s_per_hour_per_200khz"]
    bands = [band for band in BANDS_SE if band.key in band_keys]
    channels = sum(afa_channels(band, phy.bandwidth_hz) for band in bands)
    portions = sum((band.high_hz - band.low_hz) / _PORTION_HZ for band in bands)
    need_s = traffic.airtime_fraction(phy) * HOUR_S
    shortest = min((phy.time_on_air_s(size) for size in traffic.packets), default=0)
    longest = traffic.max_packet_s(phy)
    # Back-to-back fragments: every channel must rest Toff_min before reuse.
    toff = POLITE_ACCESS["min_off_time_same_frequency_s"]
    rotation = math.ceil(toff / shortest) + 1 if shortest else 0
    conservative_s = channels * per_portion
    upper_s = portions * per_portion
    return {
        "phy": phy.describe(),
        "bands": "+".join(band.key for band in bands),
        "need_s_per_hour": round(need_s),
        "channels_available": channels,
        "channels_needed": math.ceil(need_s / per_portion - 1e-9),
        "conservative_capacity_s_per_hour": round(conservative_s),
        "upper_bound_s_per_hour": round(upper_s),
        "fits_conservative": need_s <= conservative_s,
        "fits_upper_bound": need_s <= upper_s,
        "min_channels_in_rotation": rotation,
        "single_transmission_ok": longest <= POLITE_ACCESS["max_single_transmission_s"],
        "cca_threshold_dbm": round(cca_threshold_dbm(phy.bandwidth_hz, 25), 1),
    }


# --------------------------------------------------------------------------
# Link budget and a flat-ground range estimate
# --------------------------------------------------------------------------

#: Demodulator SNR limits from the Semtech SX1261/2 datasheet.
LORA_SNR_LIMIT_DB = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0}
LORA_SNR_LIMIT_DB |= {11: -17.5, 12: -20.0}
#: Receiver noise figure that reproduces the datasheet -124 dBm at SF7/BW125.
SX1262_NOISE_FIGURE_DB = 6.5


def sensitivity_dbm(phy: LoRaPhy, noise_figure_db: float = SX1262_NOISE_FIGURE_DB):
    return (
        -174
        + 10 * math.log10(phy.bandwidth_hz)
        + noise_figure_db
        + LORA_SNR_LIMIT_DB[phy.spreading_factor]
    )


def max_path_loss_db(erp: float, phy: LoRaPhy, rx_gain_dbi: float = DIPOLE_GAIN_DBI):
    """Largest tolerable path loss from transmit e.r.p. to the receiver input."""
    return erp + DIPOLE_GAIN_DBI + rx_gain_dbi - sensitivity_dbm(phy)


def two_ray_range_m(path_loss_db: float, h_tx_m: float, h_rx_m: float, margin_db=0.0):
    """Flat-earth two-ray range: L = 40 log10 d - 20 log10(h1 h2), beyond breakpoint.

    It ignores buildings, vegetation and terrain.  Use it to compare plans, not
    as a range prediction.  Put obstacles and fading into ``margin_db``.
    """
    return 10 ** ((path_loss_db - margin_db + 20 * math.log10(h_tx_m * h_rx_m)) / 40)


#: (label, e.r.p. dBm, PHY) for range comparison.
RANGE_CASES = (
    ("today, band M, 10 mW, SF5/BW500", 10.0, LoRaPhy(5, 500_000)),
    ("band P, 5 mW, SF5/BW250", mw_to_dbm(5), LoRaPhy(5, 250_000)),
    ("band P, 2.5 mW (+4 dBm), SF5/BW250", 4.0, LoRaPhy(5, 250_000)),
    ("band P, 5 mW, SF6/BW250", mw_to_dbm(5), LoRaPhy(6, 250_000)),
    ("LBT+AFA K/L/M, 25 mW, SF5/BW500", mw_to_dbm(25), LoRaPhy(5, 500_000)),
    ("LBT+AFA K/L/M, 25 mW, SF5/BW250", mw_to_dbm(25), LoRaPhy(5, 250_000)),
    ("band O, 500 mW, SF5/BW125 (10 %)", mw_to_dbm(500), LoRaPhy(5, 125_000)),
)


def range_table(h_tx_m: float, h_rx_m: float, margins=(10.0, 25.0)) -> list[dict]:
    rows = []
    for label, erp, phy in RANGE_CASES:
        loss = max_path_loss_db(erp, phy)
        rows.append(
            {
                "case": label,
                "erp_dbm": round(erp, 2),
                "sensitivity_dbm": round(sensitivity_dbm(phy), 1),
                "max_path_loss_db": round(loss, 1),
                "range_m": {
                    str(margin): round(two_ray_range_m(loss, h_tx_m, h_rx_m, margin))
                    for margin in margins
                },
            }
        )
    return rows


def _format(report: list[dict], scenario: RtcmScenario | None, rtcm_rate: float) -> str:
    lines = []
    if scenario is not None:
        lines.append(f"RTCM scenario {scenario.key}: {scenario.description}")
    lines.append(f"RTCM source rate {rtcm_rate:.0f} B/s")
    for row in report:
        lines.append("")
        lines.append(f"[{row['plan']}] {row['description']}")
        lines.append(
            f"  {row['phy']} at {row['center_mhz']:.3f} MHz, "
            f"e.r.p. {row['base']['erp_dbm']:.1f} dBm = {row['base']['erp_mw']:.1f} mW "
            "(assumed 0 dBd antenna)"
        )
        capacity = "OK" if row["capacity_ok"] else "over planning ceiling"
        lines.append(
            f"  shared channel occupancy {100 * row['shared_channel_occupancy']:.1f} % "
            f"(planning ceiling {100 * PRACTICAL_CHANNEL_OCCUPANCY:.0f} %: {capacity})"
        )
        for side in ("base", "robot"):
            item = row[side]
            lines.append(
                f"  {side}: airtime {100 * item['airtime_fraction']:.2f} % "
                f"({item['airtime_s_per_hour']:.0f} s/h), longest packet "
                f"{item['longest_packet_ms']:.1f} ms"
            )
            if not item["bands"]:
                lines.append("    does not fit any non-specific SRD band")
            for band in item["bands"]:
                duty = (
                    "no duty-cycle limit"
                    if band["duty_limit"] is None
                    else f"limit {100 * band['duty_limit']:g} % "
                    f"({band['duty_budget_s_per_hour']:.0f} s/h) -> "
                    f"{band['duty_overrun_factor']}x"
                )
                verdict = "OK" if band["lawful_by_duty_cycle"] else "NOT OK"
                erp = (
                    "" if band["erp_ok"] else f", e.r.p. over {band['erp_limit_mw']} mW"
                )
                lines.append(
                    f"    band {band['band']} ({band['source']}), edge margin "
                    f"{band['edge_margin_khz']} kHz: {duty}{erp}: {verdict}"
                )
        legal = "within" if row["lawful"] else "OUTSIDE"
        usable = "usable" if row["lawful"] and row["capacity_ok"] else "not usable"
        lines.append(f"  => {legal} calculated limits; {usable} for this traffic")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Airtime/e.r.p./band calculator; arithmetic only, not a compliance test"
    )
    parser.add_argument(
        "--scenario",
        choices=[s.key for s in RTCM_SCENARIOS],
        default="live",
        help="RTCM content model (default: live)",
    )
    parser.add_argument(
        "--rtcm-recording",
        type=Path,
        help="use exact frame sizes and rate from an MLR1 recording instead",
    )
    parser.add_argument("--telemetry-hz", type=float, default=2.0)
    parser.add_argument("--plan", action="append", choices=[p.key for p in PLANS])
    parser.add_argument("--antenna-gain-dbi", type=float, default=DIPOLE_GAIN_DBI)
    parser.add_argument("--loss-db", type=float, default=0.0)
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument(
        "--polite-access",
        action="store_true",
        help="multi-channel LBT+AFA capacity in bands K/L/M instead of the plans",
    )
    parser.add_argument(
        "--range",
        action="store_true",
        help="link budget and flat-ground two-ray range comparison",
    )
    parser.add_argument("--base-height-m", type=float, default=1.5)
    parser.add_argument("--robot-height-m", type=float, default=0.3)
    parser.add_argument("--json", action="store_true")
    return parser


def _print_rows(rows, args, render) -> None:
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("\n".join(render(row) for row in rows))


def _polite_line(row: dict) -> str:
    verdict = (
        "fits"
        if row["fits_conservative"]
        else "fits only if a lab accepts the upper bound"
        if row["fits_upper_bound"]
        else "does not fit"
    )
    return (
        f"{row['phy']:24} need {row['need_s_per_hour']:5} s/h, "
        f"{row['channels_needed']:2} channels of {row['channels_available']:2} "
        f"(conservative {row['conservative_capacity_s_per_hour']} s/h, "
        f"upper {row['upper_bound_s_per_hour']} s/h): {verdict}; "
        f"rotation >= {row['min_channels_in_rotation']}, "
        f"CCA <= {row['cca_threshold_dbm']} dBm"
    )


def _range_line(row: dict) -> str:
    ranges = ", ".join(f"{m} dB margin {d} m" for m, d in row["range_m"].items())
    return (
        f"{row['case']:38} sens {row['sensitivity_dbm']:6.1f} dBm, "
        f"max loss {row['max_path_loss_db']:5.1f} dB: {ranges}"
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_scenarios:
        for scenario in RTCM_SCENARIOS:
            rate = rtcm_bytes_per_second(scenario.messages)
            print(f"{scenario.key:16} {rate:7.0f} B/s  {scenario.description}")
        return 0
    if args.telemetry_hz < 0:
        print("--telemetry-hz must be non-negative", file=sys.stderr)
        return 2
    if args.range:
        if args.base_height_m <= 0 or args.robot_height_m <= 0:
            print("antenna heights must be positive", file=sys.stderr)
            return 2
        rows = range_table(args.base_height_m, args.robot_height_m)
        _print_rows(rows, args, _range_line)
        return 0
    scenario = None
    if args.rtcm_recording:
        messages, _ = recording_messages(args.rtcm_recording)
    else:
        scenario = next(s for s in RTCM_SCENARIOS if s.key == args.scenario)
        messages = list(scenario.messages)
    rtcm = rtcm_traffic(messages)
    telemetry = telemetry_traffic(args.telemetry_hz)
    if args.polite_access:
        rows = [
            polite_access_plan(LoRaPhy(5, bandwidth), rtcm)
            for bandwidth in (500_000, 250_000, 125_000)
        ]
        _print_rows(rows, args, _polite_line)
        return 0
    selected = [p for p in PLANS if not args.plan or p.key in args.plan]
    report = []
    for plan in selected:
        emission = plan.emission
        adjusted = Plan(
            plan.key,
            plan.description,
            Emission(
                emission.center_hz,
                emission.phy,
                emission.conducted_dbm,
                args.antenna_gain_dbi,
                args.loss_db,
                emission.obw_factor,
                emission.tolerance_ppm,
            ),
        )
        report.append(assess(adjusted, rtcm, telemetry))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_format(report, scenario, rtcm_bytes_per_second(messages)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
