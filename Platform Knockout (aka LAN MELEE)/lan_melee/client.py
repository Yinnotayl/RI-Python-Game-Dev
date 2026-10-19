"""Non-blocking UDP client, LAN discovery, RTT measurement, and interpolation."""

from __future__ import annotations

import json
import socket
import time
from collections import deque
from dataclasses import dataclass

from .constants import (
    CLIENT_TIMEOUT,
    DISCOVERY_PORT,
    INTERPOLATION_DELAY,
    INPUT_ATTACK,
    INPUT_DASH,
    INPUT_JUMP,
)
from .protocol import (
    MSG_EVENT,
    MSG_PONG,
    MSG_REJECT,
    MSG_ROSTER,
    MSG_SNAPSHOT,
    MSG_WELCOME,
    InputPacket,
    ProtocolError,
    Snapshot,
    SnapshotPlayer,
    pack_input,
    pack_join,
    pack_leave,
    pack_ping,
    unpack_event,
    unpack_header,
    unpack_pong,
    unpack_reject,
    unpack_roster,
    unpack_snapshot,
    unpack_welcome,
)
from .server import DISCOVERY_MAGIC, DISCOVERY_QUERY


@dataclass(slots=True)
class DiscoveredServer:
    host: str
    port: int
    name: str
    players: int
    max_players: int
    last_seen: float


class LanDiscoveryBrowser:
    def __init__(self, discovery_port: int = DISCOVERY_PORT) -> None:
        self.discovery_port = discovery_port
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._socket.bind(("", 0))
        self._socket.setblocking(False)
        self._servers: dict[tuple[str, int], DiscoveredServer] = {}
        self._last_refresh = 0.0

    @property
    def servers(self) -> list[DiscoveredServer]:
        return sorted(self._servers.values(), key=lambda server: (server.name.casefold(), server.host))

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_refresh < 1.0:
            return
        self._last_refresh = now
        for host in ("255.255.255.255", "127.0.0.1"):
            try:
                self._socket.sendto(DISCOVERY_QUERY, (host, self.discovery_port))
            except OSError:
                pass

    def poll(self) -> None:
        now = time.monotonic()
        while True:
            try:
                data, address = self._socket.recvfrom(1024)
            except BlockingIOError:
                break
            except OSError:
                return
            try:
                payload = json.loads(data.decode("utf-8"))
                if payload.get("magic") != DISCOVERY_MAGIC:
                    continue
                port = int(payload["port"])
                server = DiscoveredServer(
                    address[0],
                    port,
                    str(payload["name"])[:48],
                    int(payload["players"]),
                    int(payload["max_players"]),
                    now,
                )
                self._servers[(server.host, server.port)] = server
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        self._servers = {
            key: server for key, server in self._servers.items() if now - server.last_seen < 3.5
        }

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class NetworkEvent:
    received_at: float
    event_code: int
    actor_id: int
    target_id: int


class InputTracker:
    """Turn key edges into repeated action serials so UDP loss cannot eat a press."""

    def __init__(self) -> None:
        self.previous_mask = 0
        self.jump_serial = 0
        self.dash_serial = 0
        self.attack_serial = 0

    def update(self, mask: int) -> tuple[bool, bool, bool]:
        pressed = mask & ~self.previous_mask
        jump = bool(pressed & INPUT_JUMP)
        dash = bool(pressed & INPUT_DASH)
        attack = bool(pressed & INPUT_ATTACK)
        if jump:
            self.jump_serial = (self.jump_serial + 1) & 0xFFFF
        if dash:
            self.dash_serial = (self.dash_serial + 1) & 0xFFFF
        if attack:
            self.attack_serial = (self.attack_serial + 1) & 0xFFFF
        self.previous_mask = mask
        return jump, dash, attack

    def release_all(self) -> None:
        self.previous_mask = 0


class GameClient:
    def __init__(self, host: str, port: int, player_name: str) -> None:
        self.server_address = (socket.gethostbyname(host), int(port))
        self.player_name = player_name.strip()[:24] or "Player"
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.connect(self.server_address)
        self._socket.setblocking(False)

        now = time.monotonic()
        self.started_at = now
        self.last_packet_at = now
        self.last_join_at = 0.0
        self.last_ping_at = 0.0
        self.player_id: int | None = None
        self.server_name = ""
        self.error = ""
        self.input_sequence = 0
        self.latest_snapshot: Snapshot | None = None
        self.snapshot_generation = 0
        self.snapshot_history: deque[tuple[float, Snapshot]] = deque(maxlen=20)
        self.roster: dict[int, tuple[tuple[int, int, int], str]] = {}
        self.events: deque[NetworkEvent] = deque(maxlen=12)
        self.rtt_ms: float | None = None
        self.jitter_ms = 0.0
        self._ping_id = 0
        self._pending_pings: dict[int, float] = {}
        self._closed = False
        self._send_join(now)

    @property
    def connected(self) -> bool:
        return self.player_id is not None and not self.error

    def _send(self, packet: bytes) -> None:
        if self._closed:
            return
        try:
            self._socket.send(packet)
        except (BlockingIOError, ConnectionRefusedError, OSError):
            pass

    def _send_join(self, now: float) -> None:
        self._send(pack_join(self.player_name))
        self.last_join_at = now

    def poll(self) -> None:
        if self._closed:
            return
        now = time.monotonic()
        if self.player_id is None and now - self.last_join_at >= 0.5:
            self._send_join(now)
        if self.connected and now - self.last_ping_at >= 1.0:
            self._send_ping(now)

        while True:
            try:
                data = self._socket.recv(2048)
            except BlockingIOError:
                break
            except ConnectionRefusedError:
                if now - self.started_at > 1.0:
                    self.error = "No game server replied at that address."
                break
            except OSError:
                self.error = "The network connection closed."
                break
            self.last_packet_at = now
            self._handle_packet(data, now)

        if self.connected and now - self.last_packet_at > CLIENT_TIMEOUT:
            self.error = "Connection timed out. Check Wi-Fi and firewall settings."
        elif self.player_id is None and now - self.started_at > CLIENT_TIMEOUT:
            self.error = "Could not join the server. Check the IP address and firewall."

    def _handle_packet(self, data: bytes, now: float) -> None:
        try:
            message_type, body = unpack_header(data)
            if message_type == MSG_WELCOME:
                player_id, _, _, server_name = unpack_welcome(body)
                self.player_id = player_id
                self.server_name = server_name
            elif message_type == MSG_SNAPSHOT:
                snapshot = unpack_snapshot(body)
                self.latest_snapshot = snapshot
                self.snapshot_generation += 1
                self.snapshot_history.append((now, snapshot))
            elif message_type == MSG_ROSTER:
                self.roster = unpack_roster(body)
            elif message_type == MSG_EVENT:
                event_code, actor_id, target_id = unpack_event(body)
                self.events.append(NetworkEvent(now, event_code, actor_id, target_id))
            elif message_type == MSG_PONG:
                ping_id, _ = unpack_pong(body)
                sent_at = self._pending_pings.pop(ping_id, None)
                if sent_at is not None:
                    sample = max(0.0, (now - sent_at) * 1000)
                    if self.rtt_ms is None:
                        self.rtt_ms = sample
                    else:
                        difference = abs(sample - self.rtt_ms)
                        self.jitter_ms = self.jitter_ms * 0.75 + difference * 0.25
                        self.rtt_ms = self.rtt_ms * 0.8 + sample * 0.2
            elif message_type == MSG_REJECT:
                self.error = unpack_reject(body)
        except ProtocolError:
            return

    def _send_ping(self, now: float) -> None:
        self._ping_id = (self._ping_id + 1) & 0xFFFFFFFF
        client_ms = int(now * 1000) & 0xFFFFFFFF
        self._pending_pings[self._ping_id] = now
        self._send(pack_ping(self._ping_id, client_ms))
        self.last_ping_at = now
        cutoff = now - 5.0
        self._pending_pings = {
            ping_id: sent for ping_id, sent in self._pending_pings.items() if sent >= cutoff
        }

    def send_input(self, mask: int, tracker: InputTracker) -> int:
        self.input_sequence = (self.input_sequence + 1) & 0xFFFFFFFF
        now_ms = int(time.monotonic() * 1000) & 0xFFFFFFFF
        packet = InputPacket(
            self.input_sequence,
            mask,
            tracker.jump_serial,
            tracker.dash_serial,
            tracker.attack_serial,
            now_ms,
        )
        self._send(pack_input(packet))
        return self.input_sequence

    def interpolated_players(self, now: float | None = None) -> dict[int, SnapshotPlayer]:
        if not self.snapshot_history:
            return {}
        now = time.monotonic() if now is None else now
        target_time = now - INTERPOLATION_DELAY
        history = list(self.snapshot_history)
        if len(history) == 1 or target_time <= history[0][0]:
            return {player.player_id: player for player in history[0][1].players}

        earlier_time, earlier = history[-1]
        later_time, later = history[-1]
        for index in range(1, len(history)):
            if history[index][0] >= target_time:
                earlier_time, earlier = history[index - 1]
                later_time, later = history[index]
                break
        else:
            return {player.player_id: player for player in history[-1][1].players}

        duration = max(0.0001, later_time - earlier_time)
        alpha = max(0.0, min(1.0, (target_time - earlier_time) / duration))
        earlier_map = {player.player_id: player for player in earlier.players}
        output: dict[int, SnapshotPlayer] = {}
        for newer in later.players:
            older = earlier_map.get(newer.player_id, newer)
            output[newer.player_id] = SnapshotPlayer(
                newer.player_id,
                older.x + (newer.x - older.x) * alpha,
                older.y + (newer.y - older.y) * alpha,
                older.vx + (newer.vx - older.vx) * alpha,
                older.vy + (newer.vy - older.vy) * alpha,
                newer.health,
                newer.flags,
                newer.facing_right,
                newer.attack_fraction,
                newer.kills,
                newer.deaths,
                newer.acknowledged_input,
            )
        return output

    def close(self) -> None:
        if self._closed:
            return
        if self.player_id is not None:
            self._send(pack_leave())
        self._closed = True
        try:
            self._socket.close()
        except OSError:
            pass
