"""Role-specific configuration, intentionally small and explicit."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModemConfig:
    device: str
    baudrate: int = 115200
    reconnect_seconds: float = 1.0


@dataclass(frozen=True)
class ServiceConfig:
    role: str
    modem: ModemConfig
    metrics_bind: str = "127.0.0.1"
    metrics_port: int = 9608
    logging_level: str = "INFO"
    rtcm_input: dict[str, Any] = field(default_factory=dict)
    rtcm_output: dict[str, Any] = field(default_factory=dict)


def load(path: str | Path) -> ServiceConfig:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - installation error
        raise RuntimeError("install host dependencies: pip install .") from exc
    data = yaml.safe_load(Path(path).read_text()) or {}
    role = data.get("role")
    if role not in {"base", "robot"}:
        raise ValueError("role must be base or robot")
    modem = data.get("modem") or {}
    if not isinstance(modem.get("device"), str) or not modem["device"].startswith(
        "/dev/serial/by-id/"
    ):
        raise ValueError("modem.device must use stable /dev/serial/by-id/... path")
    metrics = data.get("metrics") or {}
    rtcm = data.get("rtcm") or {}
    logging_config = data.get("logging") or {}
    baudrate = int(modem.get("baudrate", 115200))
    reconnect_seconds = float(modem.get("reconnect_seconds", 1.0))
    metrics_port = int(metrics.get("port", 9608))
    level = str(logging_config.get("level", "INFO")).upper()
    if (
        baudrate <= 0
        or not math.isfinite(reconnect_seconds)
        or not 0 <= reconnect_seconds <= 60
    ):
        raise ValueError("modem baudrate/reconnect_seconds out of range")
    if not 1 <= metrics_port <= 65535 or level not in {
        "CRITICAL",
        "ERROR",
        "WARNING",
        "INFO",
        "DEBUG",
    }:
        raise ValueError("metrics port or logging.level invalid")
    return ServiceConfig(
        role=role,
        modem=ModemConfig(
            modem["device"],
            baudrate,
            reconnect_seconds,
        ),
        metrics_bind=str(metrics.get("bind", "127.0.0.1")),
        metrics_port=metrics_port,
        logging_level=level,
        rtcm_input=dict(rtcm.get("input") or {}),
        rtcm_output=dict(rtcm.get("output") or {}),
    )
