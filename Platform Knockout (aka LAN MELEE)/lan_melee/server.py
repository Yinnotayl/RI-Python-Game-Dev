"""Authoritative low-latency UDP game server and LAN discovery responder."""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from dataclasses import dataclass
from typing import Final

from .constants import (
    CLIENT_TIMEOUT,
    DISCOVERY_PORT,
    EVENT_HIT,
    EVENT_KILL,
    FIXED_DT,
    FPS,
    GAME_PORT,
    MAX_PLAYERS,
    PHASE_ACTIVE,
    PHASE_ROUND_OVER,
    PHASE_WAITING,
    PLAYER_COLORS,
    ROUND_OVER_SECONDS,
    SNAPSHOT_RATE,
    SPAWN_POINTS,
)
from .physics import ControlFrame, PlayerState, resolve_combat, simulate_player
from .protocol import (
    MSG_INPUT,
    MSG_JOIN,
    MSG_LEAVE,
    MSG_PING,
    ProtocolError,
    pack_event,
    pack_pong,
    pack_reject,
    pack_roster,
    pack_snapshot,
    pack_welcome,
    unpack_header,
    unpack_input,
    unpack_join,
    unpack_ping,
)

DISCOVERY_QUERY: Final[bytes] = b"LAN_MELEE_DISCOVER_V1"
DISCOVERY_MAGIC: Final[str] = "LAN_MELEE_V1"


def local_lan_ip() -> str:
    """Return the address other machines should normally use to reach this host."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return str(probe.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        probe.close()


def _clean_name(value: str, fallback: str, limit: int) -> str:
    cleaned = "".join(character for character in value.strip() if character.isprintable())
    return (cleaned or fallback)[:limit]


def _is_newer_sequence(sequence: int, previous: int) -> bool:
    delta = (sequence - previous) & 0xFFFFFFFF
    return 0 < delta < 0x80000000


@dataclass(slots=True)
class Peer:
    address: tuple[str, int]
    player_id: int
    name: str
    last_seen: float
    input_mask: int = 0
    input_sequence: int = 0
    jump_serial: int = 0
    dash_serial: int = 0
    attack_serial: int = 0
    consumed_jump_serial: int = 0
    consumed_dash_serial: int = 0
    consumed_attack_serial: int = 0

    def controls(self) -> ControlFrame:
        jump = self.jump_serial != self.consumed_jump_serial
        dash = self.dash_serial != self.consumed_dash_serial
        attack = self.attack_serial != self.consumed_attack_serial
        self.consumed_jump_serial = self.jump_serial
        self.consumed_dash_serial = self.dash_serial
        self.consumed_attack_serial = self.attack_serial
        return ControlFrame(self.input_mask, jump, dash, attack)


class LocalGameServer:
    """A server that can run in a background thread or as a dedicated process."""

    def __init__(
        self,
        server_name: str = "LAN Arena",
        bind_host: str = "0.0.0.0",
        game_port: int = GAME_PORT,
        discovery_port: int = DISCOVERY_PORT,
        max_players: int = MAX_PLAYERS,
    ) -> None:
        self.server_name = _clean_name(server_name, "LAN Arena", 48)
        self.bind_host = bind_host
        self.discovery_port = discovery_port
        self.max_players = max(2, min(MAX_PLAYERS, max_players))
        self.lan_ip = local_lan_ip()

        self._game_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._game_socket.bind((bind_host, game_port))
        self.game_port = int(self._game_socket.getsockname()[1])
        self._game_socket.setblocking(False)

        self._discovery_socket: socket.socket | None = None
        try:
            if discovery_port >= 0:
                discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                discovery_socket.bind((bind_host, discovery_port))
                discovery_socket.setblocking(False)
                self._discovery_socket = discovery_socket
        except OSError:
            self._game_socket.close()
            raise

        self._peers: dict[tuple[str, int], Peer] = {}
        self._players: dict[int, PlayerState] = {}
        self._next_player_id = 1
        self._tick_number = 0
        self._round_id = 1
        self._phase = PHASE_WAITING
        self._winner_id = -1
        self._round_ends_at = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def player_count(self) -> int:
        return len(self._peers)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.serve_forever, name="lan-melee-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._close_sockets()

    def _close_sockets(self) -> None:
        for game_socket in (self._game_socket, self._discovery_socket):
            if game_socket is not None:
                try:
                    game_socket.close()
                except OSError:
                    pass
        self._discovery_socket = None

    def serve_forever(self) -> None:
        next_tick = time.monotonic()
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                wait = max(0.0, min(0.05, next_tick - now))
                sockets = [self._game_socket]
                if self._discovery_socket is not None:
                    sockets.append(self._discovery_socket)
                try:
                    readable, _, _ = select.select(sockets, [], [], wait)
                except (OSError, ValueError):
                    if self._stop_event.is_set():
                        break
                    raise

                if self._game_socket in readable:
                    self._drain_game_socket()
                if self._discovery_socket is not None and self._discovery_socket in readable:
                    self._drain_discovery_socket()

                now = time.monotonic()
                steps = 0
                while now >= next_tick and steps < 5:
                    self._tick(next_tick)
                    next_tick += FIXED_DT
                    steps += 1
                if steps == 5 and now >= next_tick:
                    next_tick = now + FIXED_DT
        finally:
            self._close_sockets()

    def _drain_game_socket(self) -> None:
        while True:
            try:
                data, address = self._game_socket.recvfrom(2048)
            except BlockingIOError:
                break
            except OSError:
                return
            self._handle_game_packet(data, address, time.monotonic())

    def _handle_game_packet(self, data: bytes, address: tuple[str, int], now: float) -> None:
        try:
            message_type, body = unpack_header(data)
            if message_type == MSG_JOIN:
                self._handle_join(address, unpack_join(body), now)
                return

            peer = self._peers.get(address)
            if peer is None:
                return
            peer.last_seen = now

            if message_type == MSG_INPUT:
                packet = unpack_input(body)
                if _is_newer_sequence(packet.sequence, peer.input_sequence):
                    peer.input_sequence = packet.sequence
                    peer.input_mask = packet.mask
                    peer.jump_serial = packet.jump_serial
                    peer.dash_serial = packet.dash_serial
                    peer.attack_serial = packet.attack_serial
            elif message_type == MSG_PING:
                ping_id, client_ms = unpack_ping(body)
                self._send(pack_pong(ping_id, client_ms), address)
            elif message_type == MSG_LEAVE:
                self._remove_peer(address)
        except ProtocolError:
            return

    def _handle_join(self, address: tuple[str, int], requested_name: str, now: float) -> None:
        existing = self._peers.get(address)
        if existing is not None:
            existing.last_seen = now
            self._send_welcome(existing)
            self._send_roster_to(address)
            return
        if len(self._peers) >= self.max_players:
            self._send(pack_reject("This arena is full."), address)
            return

        name = self._unique_name(_clean_name(requested_name, "Player", 24))
        player_id = self._next_player_id
        self._next_player_id = (self._next_player_id + 1) & 0xFFFF
        if self._next_player_id == 0:
            self._next_player_id = 1

        spawn_x, spawn_y = SPAWN_POINTS[(player_id - 1) % len(SPAWN_POINTS)]
        player = PlayerState(player_id, float(spawn_x), float(spawn_y))
        if self._phase != PHASE_WAITING:
            player.alive = False
            player.health = 0
        peer = Peer(address, player_id, name, now)
        self._peers[address] = peer
        self._players[player_id] = player
        self._send_welcome(peer)
        self._broadcast_roster()

        if self._phase == PHASE_WAITING and len(self._peers) >= 2:
            self._phase = PHASE_ACTIVE
        self._broadcast_snapshot(now)

    def _unique_name(self, requested: str) -> str:
        used = {peer.name.casefold() for peer in self._peers.values()}
        if requested.casefold() not in used:
            return requested
        index = 2
        while True:
            suffix = f" #{index}"
            candidate = requested[: 24 - len(suffix)] + suffix
            if candidate.casefold() not in used:
                return candidate
            index += 1

    def _send_welcome(self, peer: Peer) -> None:
        self._send(
            pack_welcome(peer.player_id, self._tick_number, self._round_id, self.server_name),
            peer.address,
        )

    def _roster_packet(self) -> bytes:
        rows = []
        for index, peer in enumerate(sorted(self._peers.values(), key=lambda item: item.player_id)):
            color = PLAYER_COLORS[index % len(PLAYER_COLORS)]
            rows.append((peer.player_id, color, peer.name))
        return pack_roster(rows)

    def _send_roster_to(self, address: tuple[str, int]) -> None:
        self._send(self._roster_packet(), address)

    def _broadcast_roster(self) -> None:
        self._broadcast(self._roster_packet())

    def _tick(self, tick_time: float) -> None:
        self._tick_number = (self._tick_number + 1) & 0xFFFFFFFF
        self._remove_timed_out_peers(tick_time)

        for peer in tuple(self._peers.values()):
            player = self._players.get(peer.player_id)
            if player is None:
                continue
            player.last_input_sequence = peer.input_sequence
            simulate_player(player, peer.controls())

        if self._phase != PHASE_ROUND_OVER:
            for attacker_id, target_id, killed in resolve_combat(self._players.values()):
                event_code = EVENT_KILL if killed else EVENT_HIT
                self._broadcast(pack_event(event_code, attacker_id, target_id))
            self._evaluate_round(tick_time)
        elif tick_time >= self._round_ends_at:
            self._start_next_round()

        snapshots_per_tick = max(1, FPS // SNAPSHOT_RATE)
        if self._tick_number % snapshots_per_tick == 0:
            self._broadcast_snapshot(tick_time)

    def _evaluate_round(self, now: float) -> None:
        connected = len(self._players)
        if connected < 2:
            if self._phase != PHASE_WAITING:
                self._phase = PHASE_WAITING
                self._winner_id = -1
                for index, player in enumerate(self._players.values()):
                    if not player.alive:
                        player.respawn(*SPAWN_POINTS[index % len(SPAWN_POINTS)])
            return
        if self._phase == PHASE_WAITING:
            self._phase = PHASE_ACTIVE
        if self._phase == PHASE_ACTIVE:
            alive = [player.player_id for player in self._players.values() if player.alive]
            if len(alive) <= 1:
                self._phase = PHASE_ROUND_OVER
                self._winner_id = alive[0] if alive else -1
                self._round_ends_at = now + ROUND_OVER_SECONDS

    def _start_next_round(self) -> None:
        self._round_id = (self._round_id + 1) & 0xFFFF
        if self._round_id == 0:
            self._round_id = 1
        self._winner_id = -1
        ordered = sorted(self._players.values(), key=lambda player: player.player_id)
        for index, player in enumerate(ordered):
            player.respawn(*SPAWN_POINTS[index % len(SPAWN_POINTS)])
        self._phase = PHASE_ACTIVE if len(ordered) >= 2 else PHASE_WAITING

    def _broadcast_snapshot(self, now: float) -> None:
        remaining_ms = 0
        if self._phase == PHASE_ROUND_OVER:
            remaining_ms = round(max(0.0, self._round_ends_at - now) * 1000)
        packet = pack_snapshot(
            self._tick_number,
            self._round_id,
            self._phase,
            remaining_ms,
            self._winner_id,
            sorted(self._players.values(), key=lambda player: player.player_id),
        )
        self._broadcast(packet)

    def _remove_timed_out_peers(self, now: float) -> None:
        stale = [address for address, peer in self._peers.items() if now - peer.last_seen > CLIENT_TIMEOUT]
        for address in stale:
            self._remove_peer(address)

    def _remove_peer(self, address: tuple[str, int]) -> None:
        peer = self._peers.pop(address, None)
        if peer is None:
            return
        self._players.pop(peer.player_id, None)
        self._broadcast_roster()
        self._evaluate_round(time.monotonic())

    def _drain_discovery_socket(self) -> None:
        if self._discovery_socket is None:
            return
        while True:
            try:
                data, address = self._discovery_socket.recvfrom(512)
            except BlockingIOError:
                break
            except OSError:
                return
            if data != DISCOVERY_QUERY:
                continue
            response = json.dumps(
                {
                    "magic": DISCOVERY_MAGIC,
                    "name": self.server_name,
                    "port": self.game_port,
                    "players": len(self._peers),
                    "max_players": self.max_players,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                self._discovery_socket.sendto(response, address)
            except OSError:
                pass

    def _send(self, packet: bytes, address: tuple[str, int]) -> None:
        try:
            self._game_socket.sendto(packet, address)
        except OSError:
            pass

    def _broadcast(self, packet: bytes) -> None:
        for peer in tuple(self._peers.values()):
            self._send(packet, peer.address)
