"""Pygame presentation layer: title, server select, playing, and spectating."""

from __future__ import annotations

import math
import socket
import time
from dataclasses import dataclass
from enum import Enum, auto

import pygame

from .client import GameClient, InputTracker, LanDiscoveryBrowser
from .constants import (
    ATTACK_FRAMES,
    ATTACK_HEIGHT,
    ATTACK_WIDTH,
    CROUCH_HEIGHT,
    EVENT_KILL,
    FIXED_DT,
    FLAG_ALIVE,
    FLAG_ATTACKING,
    FLAG_CROUCHING,
    FLAG_DASHING,
    FLAG_ON_GROUND,
    GAME_PORT,
    HEIGHT,
    INPUT_ATTACK,
    INPUT_CROUCH,
    INPUT_DASH,
    INPUT_JUMP,
    INPUT_LEFT,
    INPUT_RIGHT,
    PASSABLE_PLATFORMS,
    PHASE_ACTIVE,
    PHASE_ROUND_OVER,
    PHASE_WAITING,
    PLAYER_COLORS,
    PLAYER_HEIGHT,
    PLAYER_WIDTH,
    SOLID_PLATFORMS,
    WIDTH,
)
from .physics import ControlFrame, PlayerState, simulate_player
from .protocol import SnapshotPlayer
from .server import LocalGameServer


class Screen(Enum):
    TITLE = auto()
    SERVER_SELECT = auto()
    PLAYING = auto()
    SPECTATING = auto()


@dataclass(slots=True)
class DisplayPlayer:
    player_id: int
    x: float
    y: float
    health: int
    flags: int
    facing_right: bool
    attack_fraction: int
    kills: int
    deaths: int


class TextInput:
    def __init__(self, rect: pygame.Rect, placeholder: str, text: str = "", limit: int = 32) -> None:
        self.rect = rect
        self.placeholder = placeholder
        self.text = text
        self.limit = limit
        self.active = False

    def handle_event(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self.active = self.rect.collidepoint(event.pos)
        if event.type != pygame.KEYDOWN or not self.active:
            return False
        if event.key == pygame.K_RETURN:
            return True
        if event.key == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
        elif event.key == pygame.K_ESCAPE:
            self.active = False
        elif event.unicode and event.unicode.isprintable() and len(self.text) < self.limit:
            self.text += event.unicode
        return False


class GameApp:
    def __init__(self) -> None:
        pygame.init()
        pygame.display.set_caption("LAN Melee")
        self.window = pygame.display.set_mode((WIDTH, HEIGHT))
        self.clock = pygame.time.Clock()
        self.font_tiny = pygame.font.SysFont("arial", 12)
        self.font_small = pygame.font.SysFont("arial", 15)
        self.font_medium = pygame.font.SysFont("arial", 22, bold=True)
        self.font_large = pygame.font.SysFont("arial", 42, bold=True)
        self.font_title = pygame.font.SysFont("arial", 60, bold=True)

        self.screen = Screen.TITLE
        self.running = True
        self.accumulator = 0.0
        self.discovery = LanDiscoveryBrowser()
        self.local_server: LocalGameServer | None = None
        self.client: GameClient | None = None
        self.input_tracker = InputTracker()
        self.predicted_player: PlayerState | None = None
        self.predicted_round_id = 0
        self.processed_snapshot_generation = 0
        self.message = ""
        self.message_until = 0.0

        self.name_input = TextInput(pygame.Rect(36, 75, 220, 34), "Player name", "Player", 24)
        self.ip_input = TextInput(pygame.Rect(36, 334, 260, 34), "192.168.1.20", "", 64)
        self._server_rows: list[tuple[pygame.Rect, str, int]] = []

    def run(self, max_frames: int | None = None) -> None:
        frame_count = 0
        try:
            while self.running and (max_frames is None or frame_count < max_frames):
                elapsed = min(0.1, self.clock.tick(120) / 1000.0)
                self._handle_events()
                self._poll_services()
                self.accumulator += elapsed
                while self.accumulator >= FIXED_DT:
                    self._fixed_update()
                    self.accumulator -= FIXED_DT
                self._draw()
                pygame.display.flip()
                frame_count += 1
        finally:
            self._shutdown()

    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
                continue
            if self.screen == Screen.TITLE:
                self._handle_title_event(event)
            elif self.screen == Screen.SERVER_SELECT:
                self._handle_server_select_event(event)
            else:
                self._handle_game_event(event)

    def _handle_title_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                self._open_server_select()
            elif event.key == pygame.K_ESCAPE:
                self.running = False
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if pygame.Rect(190, 232, 220, 48).collidepoint(event.pos):
                self._open_server_select()
            elif pygame.Rect(190, 292, 220, 42).collidepoint(event.pos):
                self.running = False

    def _open_server_select(self) -> None:
        self.screen = Screen.SERVER_SELECT
        self.discovery.refresh(force=True)

    def _handle_server_select_event(self, event: pygame.event.Event) -> None:
        name_entered = self.name_input.handle_event(event)
        ip_entered = self.ip_input.handle_event(event)
        if ip_entered:
            self._join_manual()
            return
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.screen = Screen.TITLE
            return
        if event.type != pygame.MOUSEBUTTONUP or event.button != 1:
            if name_entered:
                self.name_input.active = False
            return
        if pygame.Rect(468, 18, 96, 32).collidepoint(event.pos):
            self.screen = Screen.TITLE
        elif pygame.Rect(274, 75, 142, 34).collidepoint(event.pos):
            self._host_game()
        elif pygame.Rect(426, 75, 138, 34).collidepoint(event.pos):
            self.discovery.refresh(force=True)
            self._show_message("Searching the LAN…", 1.0)
        elif pygame.Rect(310, 334, 126, 34).collidepoint(event.pos):
            self._join_manual()
        else:
            for row, host, port in self._server_rows:
                if row.collidepoint(event.pos):
                    self._connect(host, port)
                    break

    def _handle_game_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self._leave_game()

    def _poll_services(self) -> None:
        if self.screen == Screen.SERVER_SELECT:
            self.discovery.refresh()
            self.discovery.poll()
        if self.client is None:
            return
        self.client.poll()
        if self.client.error:
            error = self.client.error
            self._leave_game(error)
            return
        if self.client.snapshot_generation != self.processed_snapshot_generation:
            self._reconcile_latest_snapshot()

    def _fixed_update(self) -> None:
        if self.client is None:
            return
        local_snapshot = self._latest_local_snapshot()
        alive = bool(local_snapshot and local_snapshot.flags & FLAG_ALIVE)
        if self.predicted_player is not None:
            alive = self.predicted_player.alive
        mask = self._read_game_input() if self.screen == Screen.PLAYING and alive else 0
        jump, dash, attack = self.input_tracker.update(mask)
        if self.client.connected:
            sequence = self.client.send_input(mask, self.input_tracker)
            if self.predicted_player is not None:
                self.predicted_player.last_input_sequence = sequence
        if self.predicted_player is not None and self.predicted_player.alive:
            simulate_player(self.predicted_player, ControlFrame(mask, jump, dash, attack))

    def _read_game_input(self) -> int:
        keys = pygame.key.get_pressed()
        mask = 0
        if keys[pygame.K_LEFT]:
            mask |= INPUT_LEFT
        if keys[pygame.K_RIGHT]:
            mask |= INPUT_RIGHT
        if keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]:
            mask |= INPUT_CROUCH
        if keys[pygame.K_z]:
            mask |= INPUT_JUMP
        if keys[pygame.K_a]:
            mask |= INPUT_DASH
        if keys[pygame.K_x]:
            mask |= INPUT_ATTACK
        return mask

    def _latest_local_snapshot(self) -> SnapshotPlayer | None:
        if self.client is None or self.client.player_id is None or self.client.latest_snapshot is None:
            return None
        return next(
            (
                player
                for player in self.client.latest_snapshot.players
                if player.player_id == self.client.player_id
            ),
            None,
        )

    def _reconcile_latest_snapshot(self) -> None:
        assert self.client is not None
        self.processed_snapshot_generation = self.client.snapshot_generation
        snapshot = self.client.latest_snapshot
        local = self._latest_local_snapshot()
        if snapshot is None or local is None:
            return
        alive = bool(local.flags & FLAG_ALIVE)
        self.screen = Screen.PLAYING if alive else Screen.SPECTATING

        if (
            self.predicted_player is None
            or snapshot.round_id != self.predicted_round_id
            or self.predicted_player.alive != alive
        ):
            self.predicted_player = self._state_from_snapshot(local)
            self.predicted_round_id = snapshot.round_id
            self.input_tracker.release_all()
            return

        predicted = self.predicted_player
        error_x = local.x - predicted.x
        error_y = local.y - predicted.y
        if math.hypot(error_x, error_y) > 70:
            predicted.x = local.x
            predicted.y = local.y
        else:
            predicted.x += error_x * 0.32
            predicted.y += error_y * 0.32
        predicted.vx = predicted.vx * 0.55 + local.vx * 0.45
        predicted.vy = predicted.vy * 0.55 + local.vy * 0.45
        predicted.health = local.health
        predicted.alive = alive
        predicted.kills = local.kills
        predicted.deaths = local.deaths
        predicted.on_ground = bool(local.flags & FLAG_ON_GROUND)
        predicted.height = CROUCH_HEIGHT if local.flags & FLAG_CROUCHING else PLAYER_HEIGHT
        if not (self._read_game_input() & (INPUT_LEFT | INPUT_RIGHT)):
            predicted.facing_right = local.facing_right
        authoritative_attack = round(local.attack_fraction * ATTACK_FRAMES / 255)
        predicted.attack_remaining = max(predicted.attack_remaining, authoritative_attack)

    @staticmethod
    def _state_from_snapshot(player: SnapshotPlayer) -> PlayerState:
        state = PlayerState(player.player_id, player.x, player.y)
        state.vx = player.vx
        state.vy = player.vy
        state.health = player.health
        state.alive = bool(player.flags & FLAG_ALIVE)
        state.height = CROUCH_HEIGHT if player.flags & FLAG_CROUCHING else PLAYER_HEIGHT
        state.facing_right = player.facing_right
        state.on_ground = bool(player.flags & FLAG_ON_GROUND)
        state.dash_remaining = 1 if player.flags & FLAG_DASHING else 0
        state.attack_remaining = round(player.attack_fraction * ATTACK_FRAMES / 255)
        state.kills = player.kills
        state.deaths = player.deaths
        state.last_input_sequence = player.acknowledged_input
        return state

    def _host_game(self) -> None:
        if not self.name_input.text.strip():
            self._show_message("Enter a player name first.", 2.5)
            return
        try:
            self.local_server = LocalGameServer(f"{self.name_input.text.strip()}'s Arena")
            self.local_server.start()
        except OSError as error:
            self.local_server = None
            self._show_message(f"Could not host: {error}", 4.0)
            return
        self._connect("127.0.0.1", self.local_server.game_port)

    def _join_manual(self) -> None:
        value = self.ip_input.text.strip()
        if not value:
            self._show_message("Enter the host's LAN IP address.", 2.5)
            return
        host = value
        port = GAME_PORT
        if value.count(":") == 1:
            host, raw_port = value.rsplit(":", 1)
            try:
                port = int(raw_port)
            except ValueError:
                self._show_message("The port must be a number.", 2.5)
                return
        self._connect(host, port)

    def _connect(self, host: str, port: int) -> None:
        if not self.name_input.text.strip():
            self._show_message("Enter a player name first.", 2.5)
            return
        try:
            self.client = GameClient(host, port, self.name_input.text)
        except (OSError, ValueError) as error:
            if self.local_server is not None:
                self.local_server.stop()
                self.local_server = None
            self._show_message(f"Could not connect: {error}", 4.0)
            return
        self.input_tracker = InputTracker()
        self.predicted_player = None
        self.predicted_round_id = 0
        self.processed_snapshot_generation = 0
        self.screen = Screen.PLAYING

    def _leave_game(self, message: str = "") -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.local_server is not None:
            self.local_server.stop()
            self.local_server = None
        self.predicted_player = None
        self.input_tracker = InputTracker()
        self.screen = Screen.SERVER_SELECT
        self.discovery.refresh(force=True)
        if message:
            self._show_message(message, 4.0)

    def _draw(self) -> None:
        if self.screen == Screen.TITLE:
            self._draw_title()
        elif self.screen == Screen.SERVER_SELECT:
            self._draw_server_select()
        else:
            self._draw_game()

    def _draw_title(self) -> None:
        self._draw_menu_background()
        glow = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        pygame.draw.circle(glow, (58, 137, 255, 35), (300, 175), 150)
        self.window.blit(glow, (0, 0))
        self._text("LAN", self.font_title, (106, 190, 255), (300, 86), center=True)
        self._text("MELEE", self.font_large, (245, 248, 255), (300, 139), center=True)
        self._text("LOW-LATENCY LOCAL PVP", self.font_small, (137, 155, 184), (300, 190), center=True)
        self._button(pygame.Rect(190, 232, 220, 48), "PLAY", primary=True)
        self._button(pygame.Rect(190, 292, 220, 42), "QUIT")
        self._text("Arrow keys move  •  Z jump  •  A dash  •  X attack", self.font_tiny, (115, 131, 158), (300, 375), center=True)

    def _draw_menu_background(self) -> None:
        self.window.fill((8, 13, 25))
        for y in range(0, HEIGHT, 24):
            pygame.draw.line(self.window, (14, 23, 40), (0, y), (WIDTH, y))
        for x in range(0, WIDTH, 24):
            pygame.draw.line(self.window, (14, 23, 40), (x, 0), (x, HEIGHT))

    def _draw_server_select(self) -> None:
        self._draw_menu_background()
        self._text("SERVER SELECT", self.font_medium, (240, 244, 255), (36, 25))
        self._button(pygame.Rect(468, 18, 96, 32), "BACK")
        self._text("PLAYER NAME", self.font_tiny, (128, 147, 178), (36, 57))
        self._draw_input(self.name_input)
        self._button(pygame.Rect(274, 75, 142, 34), "HOST GAME", primary=True)
        self._button(pygame.Rect(426, 75, 138, 34), "REFRESH")

        self._text("GAMES ON YOUR LAN", self.font_tiny, (128, 147, 178), (36, 128))
        servers = self.discovery.servers[:4]
        self._server_rows = []
        if not servers:
            pygame.draw.rect(self.window, (15, 24, 42), pygame.Rect(36, 147, 528, 142), border_radius=8)
            self._text("No games found yet", self.font_small, (190, 201, 220), (300, 198), center=True)
            self._text("Host one, refresh, or enter an IP below.", self.font_tiny, (112, 132, 164), (300, 224), center=True)
        else:
            for index, server in enumerate(servers):
                row = pygame.Rect(36, 147 + index * 42, 528, 36)
                self._server_rows.append((row, server.host, server.port))
                hovered = row.collidepoint(pygame.mouse.get_pos())
                pygame.draw.rect(self.window, (27, 42, 69) if hovered else (17, 28, 48), row, border_radius=7)
                self._text(server.name, self.font_small, (235, 240, 250), (48, row.y + 8))
                self._text(f"{server.host}:{server.port}", self.font_tiny, (109, 137, 176), (278, row.y + 10))
                self._text(f"{server.players}/{server.max_players}", self.font_small, (91, 202, 255), (535, row.y + 8), center=True)

        self._text("DIRECT JOIN", self.font_tiny, (128, 147, 178), (36, 315))
        self._draw_input(self.ip_input)
        self._button(pygame.Rect(310, 334, 126, 34), "JOIN", primary=True)
        self._text("Use host IP or IP:port", self.font_tiny, (103, 120, 148), (448, 344))
        if self.message and time.monotonic() < self.message_until:
            self._text(self.message, self.font_tiny, (255, 126, 135), (300, 389), center=True)

    def _draw_input(self, input_box: TextInput) -> None:
        border = (79, 178, 255) if input_box.active else (57, 75, 105)
        pygame.draw.rect(self.window, (11, 19, 34), input_box.rect, border_radius=6)
        pygame.draw.rect(self.window, border, input_box.rect, 1, border_radius=6)
        value = input_box.text or input_box.placeholder
        color = (235, 240, 250) if input_box.text else (81, 99, 128)
        self._text(value, self.font_small, color, (input_box.rect.x + 10, input_box.rect.y + 8))
        if input_box.active and int(time.monotonic() * 2) % 2 == 0:
            width = self.font_small.size(input_box.text)[0]
            pygame.draw.line(
                self.window,
                (160, 215, 255),
                (input_box.rect.x + 11 + width, input_box.rect.y + 8),
                (input_box.rect.x + 11 + width, input_box.rect.bottom - 8),
            )

    def _draw_game(self) -> None:
        self.window.fill((8, 13, 23))
        for y in range(40, HEIGHT, 40):
            pygame.draw.line(self.window, (13, 23, 37), (0, y), (WIDTH, y))
        for x in range(0, WIDTH, 40):
            pygame.draw.line(self.window, (13, 23, 37), (x, 0), (x, HEIGHT))
        pygame.draw.line(self.window, (66, 91, 110), (0, HEIGHT - 1), (WIDTH, HEIGHT - 1), 2)

        for values in SOLID_PLATFORMS:
            rect = pygame.Rect(*values)
            pygame.draw.rect(self.window, (43, 191, 120), rect, border_radius=3)
            pygame.draw.line(self.window, (108, 242, 173), rect.topleft, rect.topright, 2)
        for values in PASSABLE_PLATFORMS:
            rect = pygame.Rect(*values)
            pygame.draw.rect(self.window, (52, 115, 214), rect, border_radius=3)
            pygame.draw.line(self.window, (105, 174, 255), rect.topleft, rect.topright, 2)

        if self.client is None or not self.client.connected or self.client.latest_snapshot is None:
            self._draw_connecting_overlay()
            return

        players = self._display_players()
        for player in players.values():
            if player.flags & FLAG_ALIVE:
                self._draw_player(player)
        self._draw_game_hud(players)

        if self.screen == Screen.SPECTATING:
            shade = pygame.Surface((WIDTH, 54), pygame.SRCALPHA)
            shade.fill((5, 7, 13, 205))
            self.window.blit(shade, (0, 46))
            self._text("ELIMINATED  •  SPECTATING", self.font_medium, (255, 112, 122), (300, 63), center=True)
            self._text("You will rejoin automatically next round", self.font_tiny, (195, 201, 217), (300, 89), center=True)

    def _display_players(self) -> dict[int, DisplayPlayer]:
        assert self.client is not None
        remote = self.client.interpolated_players()
        output = {
            player_id: DisplayPlayer(
                player.player_id,
                player.x,
                player.y,
                player.health,
                player.flags,
                player.facing_right,
                player.attack_fraction,
                player.kills,
                player.deaths,
            )
            for player_id, player in remote.items()
        }
        if self.predicted_player is not None and self.client.player_id is not None:
            state = self.predicted_player
            output[state.player_id] = DisplayPlayer(
                state.player_id,
                state.x,
                state.y,
                state.health,
                state.flags,
                state.facing_right,
                round(state.attack_remaining * 255 / ATTACK_FRAMES),
                state.kills,
                state.deaths,
            )
        return output

    def _draw_player(self, player: DisplayPlayer) -> None:
        assert self.client is not None
        color, name = self.client.roster.get(
            player.player_id,
            (PLAYER_COLORS[(player.player_id - 1) % len(PLAYER_COLORS)], f"P{player.player_id}"),
        )
        height = CROUCH_HEIGHT if player.flags & FLAG_CROUCHING else PLAYER_HEIGHT
        body = pygame.Rect(round(player.x), round(player.y), PLAYER_WIDTH, height)

        if player.flags & FLAG_ATTACKING:
            attack_y = player.y - abs(PLAYER_HEIGHT - ATTACK_HEIGHT)
            attack_x = player.x + PLAYER_WIDTH if player.facing_right else player.x - ATTACK_WIDTH
            hitbox = pygame.Rect(round(attack_x), round(attack_y), ATTACK_WIDTH, ATTACK_HEIGHT)
            attack_surface = pygame.Surface((ATTACK_WIDTH, ATTACK_HEIGHT), pygame.SRCALPHA)
            attack_surface.fill((255, 207, 68, 80))
            pygame.draw.rect(attack_surface, (255, 223, 92, 210), attack_surface.get_rect(), 2, border_radius=5)
            self.window.blit(attack_surface, hitbox.topleft)

        shadow = body.move(3, 4)
        pygame.draw.rect(self.window, (0, 0, 0, 80), shadow, border_radius=6)
        pygame.draw.rect(self.window, color, body, border_radius=6)
        highlight = tuple(min(255, component + 45) for component in color)
        pygame.draw.line(self.window, highlight, (body.x + 5, body.y + 4), (body.right - 5, body.y + 4), 2)
        if self.client.player_id == player.player_id:
            pygame.draw.rect(self.window, (247, 251, 255), body.inflate(4, 4), 2, border_radius=8)

        eye_x = body.right - 12 if player.facing_right else body.left + 8
        pygame.draw.rect(self.window, (9, 15, 24), (eye_x, body.y + 12, 5, 5), border_radius=2)
        label = self.font_tiny.render(name, True, (231, 237, 248))
        self.window.blit(label, (body.centerx - label.get_width() // 2, body.y - 17))

        health_rect = pygame.Rect(body.x, body.y - 6, PLAYER_WIDTH, 3)
        pygame.draw.rect(self.window, (65, 25, 33), health_rect)
        pygame.draw.rect(
            self.window,
            (72, 224, 135) if player.health > 34 else (255, 105, 104),
            (health_rect.x, health_rect.y, round(health_rect.w * player.health / 100), health_rect.h),
        )

    def _draw_game_hud(self, players: dict[int, DisplayPlayer]) -> None:
        assert self.client is not None and self.client.latest_snapshot is not None
        snapshot = self.client.latest_snapshot
        panel = pygame.Surface((WIDTH, 38), pygame.SRCALPHA)
        panel.fill((4, 8, 15, 205))
        self.window.blit(panel, (0, 0))
        self._text(f"ROUND {snapshot.round_id}", self.font_small, (226, 233, 246), (12, 10))
        if self.local_server is not None:
            self._text(
                f"HOST {self.local_server.lan_ip}:{self.local_server.game_port}",
                self.font_tiny,
                (105, 137, 174),
                (12, 26),
            )

        if snapshot.phase == PHASE_WAITING:
            status = "WAITING FOR ANOTHER PLAYER"
            status_color = (255, 202, 89)
        elif snapshot.phase == PHASE_ROUND_OVER:
            if snapshot.winner_id == -1:
                winner = "DRAW"
            else:
                winner = self.client.roster.get(snapshot.winner_id, ((0, 0, 0), "Unknown"))[1] + " WINS"
            status = f"{winner}  •  {snapshot.phase_remaining_ms / 1000:.1f}s"
            status_color = (255, 208, 83)
        else:
            status = "FIGHT"
            status_color = (103, 220, 255)
        self._text(status, self.font_small, status_color, (300, 10), center=True)

        ping = "-- ms" if self.client.rtt_ms is None else f"{self.client.rtt_ms:.0f} ms"
        ping_color = (93, 226, 157) if self.client.rtt_ms is None or self.client.rtt_ms < 50 else (255, 198, 83)
        self._text(ping, self.font_small, ping_color, (586, 10), right=True)

        ranking = sorted(players.values(), key=lambda player: (-player.kills, player.deaths, player.player_id))
        board_height = 20 + 17 * len(ranking)
        board = pygame.Surface((142, board_height), pygame.SRCALPHA)
        board.fill((5, 9, 17, 165))
        self.window.blit(board, (448, 46))
        self._text("K   D", self.font_tiny, (115, 135, 163), (578, 50), right=True)
        for index, player in enumerate(ranking):
            _, name = self.client.roster.get(player.player_id, ((0, 0, 0), f"P{player.player_id}"))
            name = ("• " if player.player_id == self.client.player_id else "  ") + name
            self._text(name[:14], self.font_tiny, (219, 227, 241), (455, 66 + 17 * index))
            self._text(f"{player.kills}   {player.deaths}", self.font_tiny, (178, 195, 218), (578, 66 + 17 * index), right=True)

        recent_kills = [
            event
            for event in self.client.events
            if event.event_code == EVENT_KILL and time.monotonic() - event.received_at < 4.0
        ][-3:]
        for index, event in enumerate(reversed(recent_kills)):
            actor = self.client.roster.get(event.actor_id, ((0, 0, 0), f"P{event.actor_id}"))[1]
            target = self.client.roster.get(event.target_id, ((0, 0, 0), f"P{event.target_id}"))[1]
            self._text(f"{actor} eliminated {target}", self.font_tiny, (255, 151, 158), (10, 48 + index * 17))

        self._text("← → move   Z jump   A dash   X attack   Shift drop   Esc leave", self.font_tiny, (155, 170, 192), (300, 389), center=True)

    def _draw_connecting_overlay(self) -> None:
        shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        shade.fill((3, 6, 12, 205))
        self.window.blit(shade, (0, 0))
        dots = "." * (1 + int(time.monotonic() * 2) % 3)
        self._text(f"CONNECTING{dots}", self.font_medium, (103, 203, 255), (300, 181), center=True)
        self._text("UDP handshake with the host", self.font_tiny, (151, 166, 190), (300, 215), center=True)
        self._text("Esc to cancel", self.font_tiny, (104, 119, 143), (300, 246), center=True)

    def _button(self, rect: pygame.Rect, label: str, primary: bool = False) -> None:
        hovered = rect.collidepoint(pygame.mouse.get_pos())
        if primary:
            fill = (43, 151, 225) if hovered else (34, 126, 197)
            border = (104, 205, 255)
        else:
            fill = (30, 43, 65) if hovered else (20, 31, 51)
            border = (65, 86, 118)
        pygame.draw.rect(self.window, fill, rect, border_radius=7)
        pygame.draw.rect(self.window, border, rect, 1, border_radius=7)
        self._text(label, self.font_small, (242, 246, 254), rect.center, center=True)

    def _text(
        self,
        value: str,
        font: pygame.font.Font,
        color: tuple[int, int, int],
        position: tuple[int, int],
        *,
        center: bool = False,
        right: bool = False,
    ) -> None:
        surface = font.render(value, True, color)
        rect = surface.get_rect()
        if center:
            rect.center = position
        elif right:
            rect.topright = position
        else:
            rect.topleft = position
        self.window.blit(surface, rect)

    def _show_message(self, message: str, seconds: float) -> None:
        self.message = message
        self.message_until = time.monotonic() + seconds

    def _shutdown(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.local_server is not None:
            self.local_server.stop()
            self.local_server = None
        self.discovery.close()
        pygame.quit()
