"""Optional ROS 2 adapter for MowgliNext's Universal GNSS RTCM ingress."""

from __future__ import annotations

import argparse
import logging
import socket
from queue import Empty, Full, Queue
from threading import Event, Thread

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
    ) -> None:
        self.host = host
        self.port = port
        self.reconnect_seconds = reconnect_seconds
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

    def take(self, limit: int = 64) -> list[RtcmFrame]:
        output = []
        for _ in range(limit):
            try:
                output.append(self.frames.get_nowait())
            except Empty:
                break
        return output

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
