"""Compact, full-state UDP protocol.

Inputs are repeated and snapshots are self-contained, so an occasional lost
packet cannot stall the game. No pickle or executable deserialization is used.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable

from .constants import ATTACK_FRAMES, PROTOCOL_VERSION

MAGIC = b"LM"

MSG_JOIN = 1
MSG_INPUT = 2
MSG_PING = 3
MSG_LEAVE = 4

MSG_WELCOME = 10
MSG_SNAPSHOT = 11
MSG_ROSTER = 12
MSG_EVENT = 13
MSG_PONG = 14
MSG_REJECT = 15

HEADER = struct.Struct("!2sBB")
INPUT_BODY = struct.Struct("!IBHHHI")
PING_BODY = struct.Struct("!II")
WELCOME_BODY = struct.Struct("!HIH")
SNAPSHOT_HEADER = struct.Struct("!IHBHhB")
SNAPSHOT_PLAYER = struct.Struct("!HhhhhBBBBHHI")
EVENT_BODY = struct.Struct("!BHH")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InputPacket:
    sequence: int
    mask: int
    jump_serial: int
    dash_serial: int
    attack_serial: int
    client_ms: int


@dataclass(frozen=True, slots=True)
class SnapshotPlayer:
    player_id: int
    x: float
    y: float
    vx: float
    vy: float
    health: int
    flags: int
    facing_right: bool
    attack_fraction: int
    kills: int
    deaths: int
    acknowledged_input: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    server_tick: int
    round_id: int
    phase: int
    phase_remaining_ms: int
    winner_id: int
    players: tuple[SnapshotPlayer, ...]


def _header(message_type: int) -> bytes:
    return HEADER.pack(MAGIC, PROTOCOL_VERSION, message_type)


def unpack_header(data: bytes) -> tuple[int, memoryview]:
    if len(data) < HEADER.size:
        raise ProtocolError("packet is too short")
    magic, version, message_type = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ProtocolError("wrong packet magic")
    if version != PROTOCOL_VERSION:
        raise ProtocolError("protocol version mismatch")
    return message_type, memoryview(data)[HEADER.size :]


def _pack_text(message_type: int, text: str, max_bytes: int) -> bytes:
    encoded = text.encode("utf-8", "replace")[:max_bytes]
    return _header(message_type) + bytes((len(encoded),)) + encoded


def _unpack_text(body: memoryview) -> str:
    if not body:
        raise ProtocolError("missing text length")
    length = body[0]
    if len(body) != length + 1:
        raise ProtocolError("invalid text packet")
    return bytes(body[1:]).decode("utf-8", "replace")


def pack_join(name: str) -> bytes:
    return _pack_text(MSG_JOIN, name, 24)


def unpack_join(body: memoryview) -> str:
    return _unpack_text(body)


def pack_reject(reason: str) -> bytes:
    return _pack_text(MSG_REJECT, reason, 100)


def unpack_reject(body: memoryview) -> str:
    return _unpack_text(body)


def pack_input(packet: InputPacket) -> bytes:
    return _header(MSG_INPUT) + INPUT_BODY.pack(
        packet.sequence & 0xFFFFFFFF,
        packet.mask & 0xFF,
        packet.jump_serial & 0xFFFF,
        packet.dash_serial & 0xFFFF,
        packet.attack_serial & 0xFFFF,
        packet.client_ms & 0xFFFFFFFF,
    )


def unpack_input(body: memoryview) -> InputPacket:
    if len(body) != INPUT_BODY.size:
        raise ProtocolError("invalid input packet")
    return InputPacket(*INPUT_BODY.unpack(body))


def pack_ping(ping_id: int, client_ms: int) -> bytes:
    return _header(MSG_PING) + PING_BODY.pack(ping_id & 0xFFFFFFFF, client_ms & 0xFFFFFFFF)


def unpack_ping(body: memoryview) -> tuple[int, int]:
    if len(body) != PING_BODY.size:
        raise ProtocolError("invalid ping packet")
    return PING_BODY.unpack(body)


def pack_pong(ping_id: int, client_ms: int) -> bytes:
    return _header(MSG_PONG) + PING_BODY.pack(ping_id & 0xFFFFFFFF, client_ms & 0xFFFFFFFF)


def unpack_pong(body: memoryview) -> tuple[int, int]:
    return unpack_ping(body)


def pack_leave() -> bytes:
    return _header(MSG_LEAVE)


def pack_welcome(player_id: int, server_tick: int, round_id: int, server_name: str) -> bytes:
    name = server_name.encode("utf-8", "replace")[:48]
    return (
        _header(MSG_WELCOME)
        + WELCOME_BODY.pack(player_id, server_tick & 0xFFFFFFFF, round_id & 0xFFFF)
        + bytes((len(name),))
        + name
    )


def unpack_welcome(body: memoryview) -> tuple[int, int, int, str]:
    if len(body) < WELCOME_BODY.size + 1:
        raise ProtocolError("invalid welcome packet")
    player_id, server_tick, round_id = WELCOME_BODY.unpack_from(body)
    name_length = body[WELCOME_BODY.size]
    if len(body) != WELCOME_BODY.size + 1 + name_length:
        raise ProtocolError("invalid welcome name")
    start = WELCOME_BODY.size + 1
    name = bytes(body[start : start + name_length]).decode("utf-8", "replace")
    return player_id, server_tick, round_id, name


def pack_roster(entries: Iterable[tuple[int, tuple[int, int, int], str]]) -> bytes:
    rows = list(entries)
    if len(rows) > 255:
        raise ValueError("roster is too large")
    output = bytearray(_header(MSG_ROSTER))
    output.append(len(rows))
    for player_id, color, name in rows:
        encoded = name.encode("utf-8", "replace")[:24]
        output.extend(struct.pack("!HBBBB", player_id, *color, len(encoded)))
        output.extend(encoded)
    return bytes(output)


def unpack_roster(body: memoryview) -> dict[int, tuple[tuple[int, int, int], str]]:
    if not body:
        raise ProtocolError("invalid roster")
    count = body[0]
    offset = 1
    roster: dict[int, tuple[tuple[int, int, int], str]] = {}
    for _ in range(count):
        if len(body) < offset + 6:
            raise ProtocolError("truncated roster")
        player_id, red, green, blue, name_length = struct.unpack_from("!HBBBB", body, offset)
        offset += 6
        if len(body) < offset + name_length:
            raise ProtocolError("truncated roster name")
        name = bytes(body[offset : offset + name_length]).decode("utf-8", "replace")
        offset += name_length
        roster[player_id] = ((red, green, blue), name)
    if offset != len(body):
        raise ProtocolError("unexpected roster bytes")
    return roster


def _clamp_int16(value: float, scale: float) -> int:
    return max(-32768, min(32767, round(value * scale)))


def pack_snapshot(
    server_tick: int,
    round_id: int,
    phase: int,
    phase_remaining_ms: int,
    winner_id: int,
    players: Iterable[object],
) -> bytes:
    rows = list(players)
    if len(rows) > 255:
        raise ValueError("snapshot is too large")
    output = bytearray(_header(MSG_SNAPSHOT))
    output.extend(
        SNAPSHOT_HEADER.pack(
            server_tick & 0xFFFFFFFF,
            round_id & 0xFFFF,
            phase & 0xFF,
            max(0, min(65535, phase_remaining_ms)),
            winner_id,
            len(rows),
        )
    )
    for player in rows:
        attack_fraction = round(255 * player.attack_remaining / ATTACK_FRAMES)
        output.extend(
            SNAPSHOT_PLAYER.pack(
                player.player_id,
                _clamp_int16(player.x, 10.0),
                _clamp_int16(player.y, 10.0),
                _clamp_int16(player.vx, 100.0),
                _clamp_int16(player.vy, 100.0),
                max(0, min(255, player.health)),
                player.flags & 0xFF,
                int(player.facing_right),
                max(0, min(255, attack_fraction)),
                player.kills & 0xFFFF,
                player.deaths & 0xFFFF,
                player.last_input_sequence & 0xFFFFFFFF,
            )
        )
    return bytes(output)


def unpack_snapshot(body: memoryview) -> Snapshot:
    if len(body) < SNAPSHOT_HEADER.size:
        raise ProtocolError("invalid snapshot")
    server_tick, round_id, phase, remaining, winner_id, count = SNAPSHOT_HEADER.unpack_from(body)
    expected = SNAPSHOT_HEADER.size + count * SNAPSHOT_PLAYER.size
    if len(body) != expected:
        raise ProtocolError("snapshot size mismatch")
    players: list[SnapshotPlayer] = []
    offset = SNAPSHOT_HEADER.size
    for _ in range(count):
        values = SNAPSHOT_PLAYER.unpack_from(body, offset)
        offset += SNAPSHOT_PLAYER.size
        (
            player_id,
            x,
            y,
            vx,
            vy,
            health,
            flags,
            facing_right,
            attack_fraction,
            kills,
            deaths,
            acknowledged_input,
        ) = values
        players.append(
            SnapshotPlayer(
                player_id,
                x / 10.0,
                y / 10.0,
                vx / 100.0,
                vy / 100.0,
                health,
                flags,
                bool(facing_right),
                attack_fraction,
                kills,
                deaths,
                acknowledged_input,
            )
        )
    return Snapshot(server_tick, round_id, phase, remaining, winner_id, tuple(players))


def pack_event(event_code: int, actor_id: int, target_id: int) -> bytes:
    return _header(MSG_EVENT) + EVENT_BODY.pack(event_code, actor_id, target_id)


def unpack_event(body: memoryview) -> tuple[int, int, int]:
    if len(body) != EVENT_BODY.size:
        raise ProtocolError("invalid event packet")
    return EVENT_BODY.unpack(body)
