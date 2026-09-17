"""Small dependency-free Prometheus text and JSON metrics registry."""

from __future__ import annotations

import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Metrics:
    def __init__(self) -> None:
        self._values: Counter[str] = Counter()
        self._lock = threading.Lock()

    def add(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] += amount

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._values[name] = value

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return dict(self._values)

    def prometheus(self) -> str:
        return "".join(
            f"mowgli_lora_{key} {value}\n" for key, value in self.snapshot().items()
        )


def health_for_role(metrics: Metrics, role: str) -> tuple[bool, dict[str, bool]]:
    """Return conservative process health, distinct from detailed metrics."""
    values = metrics.snapshot()
    modem = bool(values.get("modem_connected", 0))
    handshake = bool(values.get("modem_handshake_complete", 0))
    radio_ready = bool(values.get("modem_radio_ready", 0))
    checks = {
        "modem_connected": modem,
        "modem_handshake_complete": handshake,
        "modem_radio_ready": radio_ready,
    }
    if role == "base":
        # A base process without an RTCM source is alive but cannot perform its
        # sole transport job, therefore report degraded rather than healthy.
        checks["rtcm_source_connected"] = bool(values.get("rtcm_source_connected", 0))
    return all(checks.values()), checks


def serve_metrics(
    metrics: Metrics, bind: str, port: int, *, role: str | None = None
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                content_type, body = "text/plain; version=0.0.4", metrics.prometheus()
            elif self.path == "/healthz":
                content_type = "application/json"
                if role is None:
                    healthy, checks = True, {}
                else:
                    healthy, checks = health_for_role(metrics, role)
                body = json.dumps(
                    {
                        "status": "ok" if healthy else "degraded",
                        "role": role,
                        "checks": checks,
                    }
                )
            else:
                self.send_error(404)
                return
            payload = body.encode()
            self.send_response(200 if self.path == "/metrics" or healthy else 503)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
