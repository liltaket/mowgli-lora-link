"""Optional ROS 2 adapter for MowgliNext's Universal GNSS RTCM ingress."""

from __future__ import annotations

import argparse
import logging
import socket
import time
from queue import Empty, Full, Queue
from threading import Event, Thread

from tools.lora_usb.application import RTCM_TRANSPORT_TTL_MS

from .rtcm import RtcmFrame, RtcmStreamParser

LOG = logging.getLogger(__name__)
DEFAULT_TOPIC = "/_gps_internal/universal/rtcm"


def populate_rtcm_message(message, frame: RtcmFrame, stamp):
    """Populate a universal_gnss_ros2/RtcmFrame-compatible object."""
    message.stamp = stamp
    message.message_type = frame.message_type
    message.data = list(frame.raw)
    return message


class RtcmTcpReader:
    """Reconnectable reader with a bounded freshness-first frame queue."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        queue_size: int = 128,
        reconnect_seconds: float = 1.0,
        monotonic=time.monotonic,
    ) -> None:
        self.host = host
        self.port = port
        self.reconnect_seconds = reconnect_seconds
        self._monotonic = monotonic
        self.frames: Queue[RtcmFrame] = Queue(maxsize=queue_size)
        self.stop_event = Event()
        self.thread = Thread(target=self._run, daemon=True)
        self.frames_received = 0
        self.frames_dropped = 0
        self.connect_failures = 0

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2.0)

    def take(self, limit: int = 64, *, now: float | None = None) -> list[RtcmFrame]:
        """Return only frames that still fit the application freshness TTL."""
        now = self._monotonic() if now is None else now
        output = []
        for _ in range(limit):
            try:
                frame = self.frames.get_nowait()
            except Empty:
                break
            if now - frame.received_at >= RTCM_TRANSPORT_TTL_MS / 1000:
                self.frames_dropped += 1
                continue
            output.append(frame)
        return output

    def _drop_queued_frames(self) -> None:
        """A TCP reconnect is a source-session boundary, not a continuation."""
        while True:
            try:
                self.frames.get_nowait()
                self.frames_dropped += 1
            except Empty:
                return

    def _put_fresh(self, frame: RtcmFrame) -> None:
        try:
            self.frames.put_nowait(frame)
        except Full:
            try:
                self.frames.get_nowait()
            except Empty:  # pragma: no cover - concurrent consumer won race
                pass
            self.frames_dropped += 1
            self.frames.put_nowait(frame)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            parser = RtcmStreamParser()
            # Never publish a complete frame from an old TCP connection after
            # source restart/reconnect. Its ROS timestamp would otherwise make
            # an old correction look newly received.
            self._drop_queued_frames()
            try:
                with socket.create_connection(
                    (self.host, self.port), timeout=2.0
                ) as source:
                    source.settimeout(0.5)
                    while not self.stop_event.is_set():
                        try:
                            chunk = source.recv(4096)
                        except TimeoutError:
                            continue
                        if not chunk:
                            break
                        for frame in parser.feed(chunk):
                            self._put_fresh(frame)
                            self.frames_received += 1
            except OSError as exc:
                self.connect_failures += 1
                LOG.warning("RTCM TCP input unavailable: %s", exc)
            finally:
                # EOF/reset is the source boundary. Drop the closed session's
                # queued frames before the reconnect wait gives the ROS timer
                # any opportunity to publish them under a fresh timestamp.
                self._drop_queued_frames()
            self.stop_event.wait(self.reconnect_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish validated LoRa RTCM to MowgliNext Universal GNSS"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2233)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    args, ros_args = parser.parse_known_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be in range 1..65535")

    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from universal_gnss_ros2.msg import RtcmFrame as RosRtcmFrame
    except ImportError as exc:  # pragma: no cover - requires ROS installation
        raise SystemExit(
            "source the Mowgli ROS 2 environment with universal_gnss_ros2 installed"
        ) from exc

    rclpy.init(args=ros_args)
    node = Node("mowgli_lora_rtcm_bridge")
    qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=50,
        reliability=ReliabilityPolicy.RELIABLE,
    )
    publisher = node.create_publisher(RosRtcmFrame, args.topic, qos)
    reader = RtcmTcpReader(args.host, args.port)
    reader.start()
    counters = {"published": 0}

    def publish_pending() -> None:
        for frame in reader.take():
            message = populate_rtcm_message(
                RosRtcmFrame(), frame, node.get_clock().now().to_msg()
            )
            publisher.publish(message)
            counters["published"] += 1

    node.create_timer(0.02, publish_pending)
    try:
        rclpy.spin(node)
    finally:
        reader.close()
        node.get_logger().info(
            "RTCM bridge stopped: "
            f"received={reader.frames_received} "
            f"published={counters['published']} dropped={reader.frames_dropped}"
        )
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
