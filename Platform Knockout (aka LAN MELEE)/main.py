"""Launch LAN Melee or a headless dedicated server."""

from __future__ import annotations

import argparse
import os


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Low-latency Pygame melee over a local LAN")
    parser.add_argument("--dedicated", action="store_true", help="run a headless dedicated server")
    parser.add_argument("--name", default="LAN Arena", help="dedicated server display name")
    parser.add_argument("--port", type=int, default=47621, help="dedicated server UDP game port")
    parser.add_argument("--max-players", type=int, default=12, help="maximum players (2-12)")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def run_dedicated(args: argparse.Namespace) -> None:
    from lan_melee.server import LocalGameServer

    server = LocalGameServer(args.name, game_port=args.port, max_players=args.max_players)
    print(f"{server.server_name} is listening on {server.lan_ip}:{server.game_port}/UDP")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


def run_game(smoke_test: bool) -> None:
    if smoke_test:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    from lan_melee.app import GameApp

    GameApp().run(max_frames=5 if smoke_test else None)


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.dedicated:
        run_dedicated(arguments)
    else:
        run_game(arguments.smoke_test)
