from __future__ import annotations

import socket
import time
import unittest

from lan_melee.client import GameClient, InputTracker, LanDiscoveryBrowser
from lan_melee.constants import FLAG_ALIVE, INPUT_RIGHT
from lan_melee.physics import ControlFrame, PlayerState, resolve_combat, simulate_player
from lan_melee.protocol import pack_snapshot, unpack_header, unpack_snapshot
from lan_melee.server import LocalGameServer


class ProtocolTests(unittest.TestCase):
    def test_snapshot_round_trip_is_compact_and_accurate(self) -> None:
        player = PlayerState(7, 123.4, 222.2, vx=4.5, vy=-2.25)
        player.health = 66
        player.kills = 3
        player.deaths = 1
        player.last_input_sequence = 998
        packet = pack_snapshot(42, 2, 1, 0, -1, [player])
        self.assertLess(len(packet), 64)
        message_type, body = unpack_header(packet)
        self.assertEqual(message_type, 11)
        decoded = unpack_snapshot(body)
        self.assertEqual(decoded.server_tick, 42)
        self.assertEqual(decoded.round_id, 2)
        self.assertEqual(len(decoded.players), 1)
        restored = decoded.players[0]
        self.assertAlmostEqual(restored.x, 123.4, places=1)
        self.assertAlmostEqual(restored.vy, -2.25, places=2)
        self.assertEqual(restored.health, 66)
        self.assertEqual(restored.acknowledged_input, 998)


class PhysicsTests(unittest.TestCase):
    def test_floor_collision(self) -> None:
        player = PlayerState(1, 20, 345, vy=8)
        simulate_player(player, ControlFrame())
        self.assertEqual(player.y, 350)
        self.assertTrue(player.on_ground)
        self.assertEqual(player.vy, 0)

    def test_one_attack_hits_each_target_only_once(self) -> None:
        attacker = PlayerState(1, 0, 350)
        attacker.facing_right = True
        attacker.attack_remaining = 10
        target = PlayerState(2, 55, 350)
        first = resolve_combat([attacker, target])
        second = resolve_combat([attacker, target])
        self.assertEqual(first, [(1, 2, False)])
        self.assertEqual(second, [])
        self.assertEqual(target.health, 66)

    def test_three_swings_eliminate_a_player(self) -> None:
        attacker = PlayerState(1, 0, 350)
        attacker.facing_right = True
        target = PlayerState(2, 55, 350)
        killed = False
        for _ in range(3):
            attacker.attack_remaining = 10
            attacker.attack_hit_ids.clear()
            events = resolve_combat([attacker, target])
            killed = events[-1][2]
        self.assertTrue(killed)
        self.assertFalse(target.alive)
        self.assertEqual(target.health, 0)
        self.assertEqual(attacker.kills, 1)
        self.assertEqual(target.deaths, 1)


class LoopbackNetworkTests(unittest.TestCase):
    def test_lan_browser_discovers_a_host(self) -> None:
        port_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port_probe.bind(("127.0.0.1", 0))
        discovery_port = port_probe.getsockname()[1]
        port_probe.close()
        server = LocalGameServer(
            "Discovery Test",
            bind_host="127.0.0.1",
            game_port=0,
            discovery_port=discovery_port,
        )
        browser = LanDiscoveryBrowser(discovery_port)
        server.start()
        deadline = time.monotonic() + 2.0
        try:
            browser.refresh(force=True)
            while time.monotonic() < deadline and not browser.servers:
                browser.poll()
                time.sleep(0.005)
            self.assertEqual(len(browser.servers), 1)
            self.assertEqual(browser.servers[0].name, "Discovery Test")
            self.assertEqual(browser.servers[0].port, server.game_port)
        finally:
            browser.close()
            server.stop()

    def test_two_clients_join_and_receive_authoritative_state(self) -> None:
        server = LocalGameServer("Test Arena", game_port=0, discovery_port=-1)
        server.start()
        first = GameClient("127.0.0.1", server.game_port, "Alpha")
        second = GameClient("127.0.0.1", server.game_port, "Beta")
        first_input = InputTracker()
        second_input = InputTracker()
        deadline = time.monotonic() + 2.5
        try:
            while time.monotonic() < deadline:
                first.poll()
                second.poll()
                first_input.update(INPUT_RIGHT)
                second_input.update(0)
                if first.connected:
                    first.send_input(INPUT_RIGHT, first_input)
                if second.connected:
                    second.send_input(0, second_input)
                if (
                    first.latest_snapshot is not None
                    and second.latest_snapshot is not None
                    and len(first.latest_snapshot.players) == 2
                    and len(second.latest_snapshot.players) == 2
                ):
                    break
                time.sleep(0.005)
            self.assertTrue(first.connected, first.error)
            self.assertTrue(second.connected, second.error)
            self.assertEqual(first.server_name, "Test Arena")
            self.assertEqual(len(first.roster), 2)
            self.assertIsNotNone(first.latest_snapshot)
            assert first.latest_snapshot is not None
            self.assertTrue(all(player.flags & FLAG_ALIVE for player in first.latest_snapshot.players))
        finally:
            first.close()
            second.close()
            server.stop()


if __name__ == "__main__":
    unittest.main()
