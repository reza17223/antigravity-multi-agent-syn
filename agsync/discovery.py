"""Zero-dependency UDP LAN beacon & discovery for agsync hubs."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

from agsync.config import detect_lan_ip
from agsync.protocol import DEFAULT_PORT

DEFAULT_DISCOVERY_PORT = 8766
MAGIC_SERVICE = "agsync-hub"


class DiscoveryBeacon:
    """Broadcasts host coordination hub info on the LAN every interval_sec."""

    def __init__(
        self,
        *,
        http_port: int = DEFAULT_PORT,
        host_agent: str = "host",
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        interval_sec: float = 2.0,
        bind_ip: str = "",
    ) -> None:
        self.http_port = int(http_port)
        self.host_agent = host_agent
        self.discovery_port = int(discovery_port)
        self.interval_sec = float(interval_sec)
        self.bind_ip = bind_ip.strip() or detect_lan_ip()
        self.broadcast_addr = "127.0.0.1" if self.bind_ip == "127.0.0.1" else "255.255.255.255"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _payload(self) -> bytes:
        data = {
            "service": MAGIC_SERVICE,
            "host_ip": self.bind_ip,
            "port": self.http_port,
            "url": f"http://{self.bind_ip}:{self.http_port}",
            "host_agent": self.host_agent,
            "machine": socket.gethostname(),
            "ts": time.time(),
        }
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    sock.sendto(self._payload(), (self.broadcast_addr, self.discovery_port))
                except OSError:
                    pass
                self._stop.wait(self.interval_sec)
        finally:
            sock.close()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agsync-beacon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def discover_hubs(
    *,
    timeout_sec: float = 3.0,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
) -> list[dict[str, Any]]:
    """Listen for UDP LAN beacons and return deduplicated hub announcements."""
    found: dict[str, dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", int(discovery_port)))
        sock.settimeout(0.5)
        deadline = time.monotonic() + float(timeout_sec)
        while time.monotonic() < deadline:
            try:
                raw, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict) or payload.get("service") != MAGIC_SERVICE:
                continue
            src_ip = addr[0]
            host_ip = str(payload.get("host_ip") or src_ip)
            port = int(payload.get("port") or DEFAULT_PORT)
            url = f"http://{host_ip}:{port}"
            payload["source_ip"] = src_ip
            payload["url"] = url
            found[url] = payload
    finally:
        sock.close()
    return list(found.values())
