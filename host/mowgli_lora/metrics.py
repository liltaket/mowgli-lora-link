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


def serve_metrics(metrics: Metrics, bind: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                content_type, body = "text/plain; version=0.0.4", metrics.prometheus()
            elif self.path == "/healthz":
                content_type, body = "application/json", json.dumps(metrics.snapshot())
            else:
                self.send_error(404)
                return
            payload = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
