import pygame
import sys
import random

# ── Constants ────────────────────────────────────────────────────────────────
WIDTH, HEIGHT = 600, 400
FPS = 60

PLAYER_SPEED       = 7
PLAYER_ACCEL       = 5
PLAYER_JUMP        = -15
GRAVITY            = 0.75
SIZE_X             = 50
SIZE_Y_BIG         = 50
SIZE_Y_SMALL       = 25

ENEMY_SIZE         = 30
ENEMY_SPEED        = 2
ENEMY_JUMP         = -13
ENEMY_SPAWN_TICKS  = 180   # frames between spawns

SWORD_W, SWORD_H   = 45, 20
SWORD_DURATION     = 15    # frames the hitbox stays active

# ── Setup ─────────────────────────────────────────────────────────────────────
pygame.init()
screen = pygame.display.set_mode((WIDTH, HEIGHT))
clock  = pygame.time.Clock()

platforms = [
    pygame.Rect(100, 300, 200, 20),
    pygame.Rect(350, 250, 150, 20),
]
one_way_platforms = [
    pygame.Rect(200, 180, 120, 20),
]

# ── State dicts ───────────────────────────────────────────────────────────────
player = dict(
    x=300, y=200, vx=0, vy=0,
    size_y=SIZE_Y_BIG,
    on_ground=False,
    drop_down=False,
    facing=1,          # 1 = right, -1 = left
)

sword = dict(active=False, timer=0, rect=None)

enemies     = []
spawn_timer = 0

# ── Helpers ───────────────────────────────────────────────────────────────────
def player_rect():
    return pygame.Rect(player["x"], player["y"], SIZE_X, player["size_y"])

def enemy_rect(e):
    return pygame.Rect(e["x"], e["y"], ENEMY_SIZE, ENEMY_SIZE)

# ── Shared physics ────────────────────────────────────────────────────────────
def collide_x(rect, vx):
    """Push rect out of solid platforms horizontally. Returns (new_x, new_vx)."""
    for p in platforms:
        if rect.colliderect(p):
            if vx > 0:
                rect.right = p.left
            elif vx < 0:
                rect.left = p.right
            vx = 0
    return rect.x, vx

def collide_y(rect, vy, drop_down=False):
    """
    Push rect out of platforms vertically.
    Returns (new_y, new_vy, on_ground).
    """
    on_ground = False

    for p in platforms:
        if rect.colliderect(p):
            if vy > 0:
                rect.bottom = p.top
                on_ground = True
            elif vy < 0:
                rect.top = p.bottom
            vy = 0

    for p in one_way_platforms:
        if rect.colliderect(p):
            prev_bottom = rect.bottom - vy   # bottom before this frame's move
            if vy > 0 and not drop_down and prev_bottom <= p.top:
                rect.bottom = p.top
                vy = 0
                on_ground = True

    return rect.y, vy, on_ground

# ── Player ────────────────────────────────────────────────────────────────────
def update_player():
    keys = pygame.key.get_pressed()

    # Horizontal input
    if keys[pygame.K_a]:
        player["vx"] -= PLAYER_ACCEL
        player["facing"] = -1
    if keys[pygame.K_d]:
        player["vx"] += PLAYER_ACCEL
        player["facing"] = 1

    player["vx"] = max(-PLAYER_SPEED, min(PLAYER_SPEED, player["vx"]))

    # Crouch / drop-through
    if keys[pygame.K_s]:
        if player["size_y"] != SIZE_Y_SMALL:
            player["y"] += SIZE_Y_SMALL
        player["size_y"]   = SIZE_Y_SMALL
        player["drop_down"] = True
    else:
        if player["size_y"] != SIZE_Y_BIG:
            player["y"] -= SIZE_Y_SMALL
        player["size_y"]   = SIZE_Y_BIG
        player["drop_down"] = False

    # Jump
    if keys[pygame.K_SPACE] and player["on_ground"]:
        player["vy"]       = PLAYER_JUMP
        player["on_ground"] = False

    # Gravity
    player["vy"] = min(player["vy"] + GRAVITY, 20)

    # ── Horizontal pass
    player["x"] += player["vx"]
    r = player_rect()
    player["x"], player["vx"] = collide_x(r, player["vx"])

    # ── Vertical pass
    player["y"] += player["vy"]
    r = player_rect()
    player["y"], player["vy"], landed = collide_y(r, player["vy"], player["drop_down"])
    player["on_ground"] = landed

    # Floor
    if player["y"] + player["size_y"] >= HEIGHT:
        player["y"]        = HEIGHT - player["size_y"]
        player["vy"]       = 0
        player["on_ground"] = True

    # Friction
    friction = 0.75 if player["on_ground"] else 0.2
    if player["vx"] > 0:
        player["vx"] = max(0.0, player["vx"] - friction)
    elif player["vx"] < 0:
        player["vx"] = min(0.0, player["vx"] + friction)

    # Screen bounds
    if player["x"] < 0:
        player["x"], player["vx"] = 0, 0
    elif player["x"] + SIZE_X > WIDTH:
        player["x"], player["vx"] = WIDTH - SIZE_X, 0

# ── Sword ─────────────────────────────────────────────────────────────────────
def swing_sword():
    """Activate sword hitbox on the side the player is facing."""
    sword["active"] = True
    sword["timer"]  = SWORD_DURATION
    _update_sword_rect()

def _update_sword_rect():
    sy = player["y"] + player["size_y"] // 4
    if player["facing"] == 1:
        sword["rect"] = pygame.Rect(player["x"] + SIZE_X, sy, SWORD_W, SWORD_H)
    else:
        sword["rect"] = pygame.Rect(player["x"] - SWORD_W, sy, SWORD_W, SWORD_H)

def update_sword():
    if not sword["active"]:
        return

    sword["timer"] -= 1
    if sword["timer"] <= 0:
        sword["active"] = False
        return

    _update_sword_rect()  # sword tracks player while active

    # Kill any enemy the hitbox overlaps
    for e in enemies[:]:
        if sword["rect"].colliderect(enemy_rect(e)):
            enemies.remove(e)

# ── Enemies ───────────────────────────────────────────────────────────────────
def spawn_enemy():
    """Spawn an enemy from the left or right edge at floor or platform height."""
    side = random.choice((-1, 1))
    ex   = -ENEMY_SIZE if side == -1 else WIDTH

    # Pick a random valid y: floor or top of a solid platform
    y_options = [HEIGHT - ENEMY_SIZE]
    for p in platforms:
        y_options.append(p.top - ENEMY_SIZE)

    enemies.append(dict(
        x=ex, y=random.choice(y_options),
        vx=0, vy=0,
        on_ground=False,
        jump_cd=random.randint(30, 90),   # frames until first jump check
    ))

def update_enemy(e):
    """Simple chase AI: run toward player, jump when player is above or on cooldown."""
    px_center = player["x"] + SIZE_X    / 2
    ex_center = e["x"]     + ENEMY_SIZE / 2

    # Walk toward player
    e["vx"] = ENEMY_SPEED if ex_center < px_center else -ENEMY_SPEED

    # Jump logic
    e["jump_cd"] -= 1
    player_is_above = player["y"] + player["size_y"] < e["y"]
    if e["on_ground"] and (player_is_above or e["jump_cd"] <= 0):
        e["vy"]      = ENEMY_JUMP
        e["on_ground"] = False
        e["jump_cd"] = random.randint(60, 120)

    # Gravity
    e["vy"] = min(e["vy"] + GRAVITY, 20)

    # Horizontal pass
    e["x"] += e["vx"]
    r = enemy_rect(e)
    e["x"], e["vx"] = collide_x(r, e["vx"])

    # Vertical pass
    e["y"] += e["vy"]
    r = enemy_rect(e)
    e["y"], e["vy"], landed = collide_y(r, e["vy"])
    e["on_ground"] = landed

    # Floor
    if e["y"] + ENEMY_SIZE >= HEIGHT:
        e["y"]        = HEIGHT - ENEMY_SIZE
        e["vy"]       = 0
        e["on_ground"] = True

def update_spawn():
    global spawn_timer
    spawn_timer += 1
    if spawn_timer >= ENEMY_SPAWN_TICKS:
        spawn_enemy()
        spawn_timer = 0

# ── Draw ──────────────────────────────────────────────────────────────────────
def draw():
    screen.fill((0, 0, 0))

    for p in platforms:
        pygame.draw.rect(screen, (0, 255, 0), p)
    for p in one_way_platforms:
        pygame.draw.rect(screen, (0, 0, 255), p)

    pygame.draw.rect(screen, (255, 0, 0),
                     (player["x"], player["y"], SIZE_X, player["size_y"]))

    if sword["active"] and sword["rect"]:
        pygame.draw.rect(screen, (180, 0, 255), sword["rect"])

    for e in enemies:
        pygame.draw.rect(screen, (255, 165, 0),
                         (e["x"], e["y"], ENEMY_SIZE, ENEMY_SIZE))

    pygame.display.flip()

# ── Main loop ─────────────────────────────────────────────────────────────────
running = True
while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            swing_sword()

    update_player()
    update_sword()
    for e in enemies:
        update_enemy(e)
    update_spawn()
    draw()

    clock.tick(FPS)

pygame.quit()
sys.exit()