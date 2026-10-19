# LAN Melee

A small, server-authoritative Pygame platform fighter for people on the same local network. One player hosts, everyone else discovers or directly joins the arena, and eliminated players automatically spectate until the next round.

## Quick start

Use Python 3.10 or newer on every computer.

```bash
python -m pip install -r requirements.txt
python main.py
```

Then:

1. The host selects **Play → Host Game**.
2. Other players select **Play**, then click the discovered game.
3. If discovery is blocked, they enter the host computer's local IP, such as `192.168.1.20`, under **Direct Join**.

All computers must be on the same normal LAN/Wi-Fi. Guest Wi-Fi often isolates devices. If the operating system asks, allow Python through the firewall on **private networks**. Manual firewall rules should allow UDP `47620` (discovery) and `47621` (gameplay).

## Controls

| Action | Key |
|---|---|
| Move | Left / Right arrows |
| Jump | Z |
| Dash | A |
| Attack | X |
| Crouch / drop through blue platform | Shift |
| Leave game | Escape |

An attack deals 34 health, so three clean hits eliminate an opponent. The last living player wins. A new round starts after three seconds, and spectators automatically return to play.

## Four screens

- **Title** — enter the game or quit.
- **Server Select** — host, discover LAN games, or join an IP directly.
- **Playing** — predicted local movement plus server-confirmed damage and round state.
- **Spectating** — the same live arena view after death or when joining mid-round.

## Dedicated host (optional)

To run the server without a game window:

```bash
python main.py --dedicated --name "Friday Arena"
```

Optional flags include `--port 47621` and `--max-players 12`.

## Why it feels responsive

- UDP avoids TCP head-of-line delays during live play.
- The server simulates at 60 ticks per second and sends full snapshots at 30 Hz.
- Local movement and attacks are predicted immediately; remote players are interpolated over a short 50 ms window.
- Button presses carry repeating action serials, so a lost packet does not lose a jump, dash, or attack.
- Health, hits, deaths, scores, and round resets are authoritative on the host, making normal client cheating and state disagreement harder.
- Packets use a compact binary format and stay comfortably below a typical network MTU.

The in-game top-right number is measured round-trip ping. Wired Ethernet or 5/6 GHz Wi-Fi, a nearby access point, and closing large downloads will improve it further.

## Tests

The tests do not need a display:

```bash
python -m unittest discover -s tests -v
```

They cover packet round-trips, movement collision, melee damage/death, and a real two-client UDP loopback session.
