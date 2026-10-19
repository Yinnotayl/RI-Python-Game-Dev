"""Deterministic fixed-step movement and authoritative melee collision."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .constants import (
    ACCELERATION,
    AIR_FRICTION,
    ATTACK_COOLDOWN_FRAMES,
    ATTACK_DAMAGE,
    ATTACK_FRAMES,
    ATTACK_HEIGHT,
    ATTACK_WIDTH,
    CROUCH_HEIGHT,
    DASH_COOLDOWN_FRAMES,
    DASH_FRAMES,
    DASH_POWER,
    FLAG_ALIVE,
    FLAG_ATTACKING,
    FLAG_CROUCHING,
    FLAG_DASHING,
    FLAG_ON_GROUND,
    GRAVITY,
    GROUND_FRICTION,
    HEIGHT,
    INPUT_CROUCH,
    INPUT_LEFT,
    INPUT_RIGHT,
    JUMP_SPEED,
    KNOCKBACK_X,
    KNOCKBACK_Y,
    MAX_FALL_SPEED,
    MOVE_SPEED,
    PASSABLE_PLATFORMS,
    PLAYER_HEIGHT,
    PLAYER_WIDTH,
    SOLID_PLATFORMS,
    WIDTH,
)


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def left(self) -> float:
        return self.x

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def top(self) -> float:
        return self.y

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def intersects(self, other: "Rect") -> bool:
        return (
            self.left < other.right
            and self.right > other.left
            and self.top < other.bottom
            and self.bottom > other.top
        )


SOLIDS = tuple(Rect(*values) for values in SOLID_PLATFORMS)
PASSABLES = tuple(Rect(*values) for values in PASSABLE_PLATFORMS)


@dataclass(slots=True)
class ControlFrame:
    mask: int = 0
    jump_pressed: bool = False
    dash_pressed: bool = False
    attack_pressed: bool = False


@dataclass(slots=True)
class PlayerState:
    player_id: int
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    height: int = PLAYER_HEIGHT
    facing_right: bool = False
    on_ground: bool = False
    alive: bool = True
    health: int = 100
    dash_remaining: int = 0
    dash_cooldown: int = 0
    attack_remaining: int = 0
    attack_cooldown: int = 0
    attack_serial: int = 0
    attack_hit_ids: set[int] = field(default_factory=set)
    kills: int = 0
    deaths: int = 0
    last_input_sequence: int = 0

    @property
    def rect(self) -> Rect:
        return Rect(self.x, self.y, PLAYER_WIDTH, self.height)

    @property
    def flags(self) -> int:
        flags = 0
        if self.alive:
            flags |= FLAG_ALIVE
        if self.height == CROUCH_HEIGHT:
            flags |= FLAG_CROUCHING
        if self.attack_remaining > 0:
            flags |= FLAG_ATTACKING
        if self.dash_remaining > 0:
            flags |= FLAG_DASHING
        if self.on_ground:
            flags |= FLAG_ON_GROUND
        return flags

    def respawn(self, x: float, y: float) -> None:
        self.x = x
        self.y = y
        self.vx = 0.0
        self.vy = 0.0
        self.height = PLAYER_HEIGHT
        self.facing_right = x < WIDTH / 2
        self.on_ground = False
        self.alive = True
        self.health = 100
        self.dash_remaining = 0
        self.dash_cooldown = 0
        self.attack_remaining = 0
        self.attack_cooldown = 0
        self.attack_hit_ids.clear()


def _blocked_while_standing(player: PlayerState, bottom: float) -> bool:
    standing = Rect(player.x, bottom - PLAYER_HEIGHT, PLAYER_WIDTH, PLAYER_HEIGHT)
    return any(standing.intersects(platform) for platform in SOLIDS)


def simulate_player(player: PlayerState, controls: ControlFrame) -> None:
    """Advance one player by one 60 Hz tick.

    Movement intentionally follows the feel of the supplied prototype. The same
    function is used on the server and by local client prediction.
    """

    if not player.alive:
        return

    mask = controls.mask
    if mask & INPUT_LEFT:
        player.vx -= ACCELERATION
        player.facing_right = False
    if mask & INPUT_RIGHT:
        player.vx += ACCELERATION
        player.facing_right = True

    if player.dash_cooldown > 0:
        player.dash_cooldown -= 1
    if player.attack_cooldown > 0:
        player.attack_cooldown -= 1

    if controls.dash_pressed and player.dash_cooldown == 0:
        player.dash_remaining = DASH_FRAMES
        player.dash_cooldown = DASH_COOLDOWN_FRAMES

    if controls.attack_pressed and player.attack_cooldown == 0:
        player.attack_remaining = ATTACK_FRAMES
        player.attack_cooldown = ATTACK_COOLDOWN_FRAMES
        player.attack_serial = (player.attack_serial + 1) & 0xFFFF
        player.attack_hit_ids.clear()

    if player.dash_remaining == 0:
        player.vx = max(-MOVE_SPEED, min(MOVE_SPEED, player.vx))
    else:
        dash_force = DASH_POWER - (DASH_FRAMES - player.dash_remaining)
        player.vx += dash_force if player.facing_right else -dash_force
        player.dash_remaining -= 1

    if player.attack_remaining > 0:
        player.attack_remaining -= 1

    bottom = player.y + player.height
    drop_down = bool(mask & INPUT_CROUCH)
    if drop_down:
        if player.height != CROUCH_HEIGHT:
            player.height = CROUCH_HEIGHT
            player.y = bottom - player.height
    elif player.height != PLAYER_HEIGHT and not _blocked_while_standing(player, bottom):
        player.height = PLAYER_HEIGHT
        player.y = bottom - player.height

    if controls.jump_pressed and player.on_ground:
        player.vy = JUMP_SPEED
        player.on_ground = False

    player.vy = min(MAX_FALL_SPEED, player.vy + GRAVITY)

    # Horizontal movement and solid-platform collision.
    player.x += player.vx
    player_rect = player.rect
    for platform in SOLIDS:
        if player_rect.intersects(platform):
            if player.vx > 0:
                player.x = platform.left - PLAYER_WIDTH
            elif player.vx < 0:
                player.x = platform.right
            player.vx = 0.0
            player_rect = player.rect

    # Vertical movement and collision.
    old_bottom = player.y + player.height
    player.y += player.vy
    player.on_ground = False
    player_rect = player.rect
    for platform in SOLIDS:
        if player_rect.intersects(platform):
            if player.vy > 0:
                player.y = platform.top - player.height
                player.on_ground = True
            elif player.vy < 0:
                player.y = platform.bottom
            player.vy = 0.0
            player_rect = player.rect

    for platform in PASSABLES:
        if (
            player.rect.intersects(platform)
            and player.vy > 0
            and not drop_down
            and old_bottom <= platform.top
        ):
            player.y = platform.top - player.height
            player.vy = 0.0
            player.on_ground = True

    if player.y + player.height >= HEIGHT:
        player.y = HEIGHT - player.height
        player.vy = 0.0
        player.on_ground = True

    friction = GROUND_FRICTION if player.on_ground else AIR_FRICTION
    if player.vx > 0:
        player.vx = max(0.0, player.vx - friction)
    elif player.vx < 0:
        player.vx = min(0.0, player.vx + friction)

    if player.x < 0:
        player.x = 0.0
        player.vx = 0.0
    elif player.x + PLAYER_WIDTH > WIDTH:
        player.x = WIDTH - PLAYER_WIDTH
        player.vx = 0.0


def attack_rect(player: PlayerState) -> Rect:
    # Preserve the prototype's slightly raised horizontal swing.
    y = player.y - abs(PLAYER_HEIGHT - ATTACK_HEIGHT)
    x = player.x + PLAYER_WIDTH if player.facing_right else player.x - ATTACK_WIDTH
    return Rect(x, y, ATTACK_WIDTH, ATTACK_HEIGHT)


def resolve_combat(players: Iterable[PlayerState]) -> list[tuple[int, int, bool]]:
    """Apply authoritative hits; return ``(attacker, target, killed)`` events."""

    player_list = list(players)
    hits: list[tuple[PlayerState, PlayerState]] = []
    for attacker in player_list:
        if not attacker.alive or attacker.attack_remaining <= 0:
            continue
        hitbox = attack_rect(attacker)
        for target in player_list:
            if (
                target.player_id == attacker.player_id
                or not target.alive
                or target.player_id in attacker.attack_hit_ids
            ):
                continue
            if hitbox.intersects(target.rect):
                attacker.attack_hit_ids.add(target.player_id)
                hits.append((attacker, target))

    events: list[tuple[int, int, bool]] = []
    for attacker, target in hits:
        if not target.alive:
            continue
        target.health = max(0, target.health - ATTACK_DAMAGE)
        target.vx = KNOCKBACK_X if attacker.facing_right else -KNOCKBACK_X
        target.vy = KNOCKBACK_Y
        killed = target.health == 0
        if killed:
            target.alive = False
            target.deaths += 1
            attacker.kills += 1
            target.vx = 0.0
            target.vy = 0.0
        events.append((attacker.player_id, target.player_id, killed))
    return events
