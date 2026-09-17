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


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return dict(value)


def _port(value: object, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be in range 1..65535")
    return port


def _positive_int(value: object, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _rtcm_config(
    role: str, rtcm: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if role == "base":
        source = _mapping(rtcm.get("input"), "rtcm.input")
        source_type = source.get("type")
        if source_type not in {"stdin", "tcp", "serial"}:
            raise ValueError("rtcm.input.type must be stdin, tcp, or serial")
        if source_type == "tcp":
            if not isinstance(source.get("host"), str) or not source["host"]:
                raise ValueError("rtcm.input.host must be a non-empty string")
            source["port"] = _port(source.get("port"), "rtcm.input.port")
        elif source_type == "serial":
            if not isinstance(source.get("device"), str) or not source["device"]:
                raise ValueError("rtcm.input.device must be a non-empty string")
            if "baudrate" not in source:
                raise ValueError("rtcm.input.baudrate is required for serial input")
            source["baudrate"] = _positive_int(
                source["baudrate"], "rtcm.input.baudrate"
            )
        return source, {}

    output = _mapping(rtcm.get("output"), "rtcm.output")
    if output.get("type") != "tcp":
        raise ValueError("rtcm.output.type must be tcp")
    if not isinstance(output.get("bind"), str) or not output["bind"]:
        raise ValueError("rtcm.output.bind must be a non-empty string")
    output["port"] = _port(output.get("port"), "rtcm.output.port")
    return {}, output


def load(path: str | Path) -> ServiceConfig:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - installation error
        raise RuntimeError("install host dependencies: pip install .") from exc
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise TypeError("configuration root must be a mapping")
    role = data.get("role")
    if role not in {"base", "robot"}:
        raise ValueError("role must be base or robot")
    modem = _mapping(data.get("modem"), "modem")
    if not isinstance(modem.get("device"), str) or not modem["device"].startswith(
        "/dev/serial/by-id/"
    ):
        raise ValueError("modem.device must use stable /dev/serial/by-id/... path")
    metrics = _mapping(data.get("metrics") or {}, "metrics")
    rtcm = _mapping(data.get("rtcm"), "rtcm")
    logging_config = _mapping(data.get("logging") or {}, "logging")
    baudrate = _positive_int(modem.get("baudrate", 115200), "modem.baudrate")
    reconnect_seconds = float(modem.get("reconnect_seconds", 1.0))
    metrics_port = _port(metrics.get("port", 9608), "metrics.port")
    level = str(logging_config.get("level", "INFO")).upper()
    if not math.isfinite(reconnect_seconds) or not 0 <= reconnect_seconds <= 60:
        raise ValueError("modem baudrate/reconnect_seconds out of range")
    if level not in {
        "CRITICAL",
        "ERROR",
        "WARNING",
        "INFO",
        "DEBUG",
    }:
        raise ValueError("metrics port or logging.level invalid")
    rtcm_input, rtcm_output = _rtcm_config(role, rtcm)
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
        rtcm_input=rtcm_input,
        rtcm_output=rtcm_output,
    )
