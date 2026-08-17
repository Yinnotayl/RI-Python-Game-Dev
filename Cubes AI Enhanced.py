import pygame
import math
import random

pygame.init()

WIDTH, HEIGHT = 800, 600

# The real OS window/display surface. We flip this one at the end of every
# frame. Everything is actually drawn onto the offscreen `screen` surface
# below so that the whole frame can be nudged around for a screen-shake
# effect during the death animation without touching any of the existing
# draw calls.
display_surface = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Cubes")

screen = pygame.Surface((WIDTH, HEIGHT))

clock = pygame.time.Clock()

FONT_NAME = "Cascadia Code"
HUD_FONT = pygame.font.SysFont(FONT_NAME, 30)
GAMEOVER_KICKER_FONT = pygame.font.SysFont(FONT_NAME, 16, bold=True)
GAMEOVER_TITLE_FONT = pygame.font.SysFont(FONT_NAME, 58, bold=True)
GAMEOVER_SCORE_FONT = pygame.font.SysFont(FONT_NAME, 112, bold=True)
GAMEOVER_LABEL_FONT = pygame.font.SysFont(FONT_NAME, 15, bold=True)
GAMEOVER_PROMPT_FONT = pygame.font.SysFont(FONT_NAME, 18, bold=True)
GAMEOVER_META_FONT = pygame.font.SysFont(FONT_NAME, 13)

CUBEDISTANCE = 200

PLAYER_CYAN = (0, 255, 255)
PLAYER_RED = (255, 0, 0)
GAMEOVER_RED = (255, 38, 62)

score: int = 0
health = 10

# The visible score is deliberately integer-only. While absorbing, one whole
# point is removed on each tick; the fractional value exists only in this
# private timer and is never added to ``score``.
ABSORB_SCORE_TICKS_PER_SECOND = 7
ABSORB_SCORE_TICK_INTERVAL = 1.0 / ABSORB_SCORE_TICKS_PER_SECOND
absorbing = False
absorb_score_timer = 0.0

DAMAGE_SHAKE_DURATION = 0.18
DAMAGE_SHAKE_STRENGTH = 9
damage_shake_timer = 0.0

# Gameplay settings
SPAWN_ORIGINAL_MIN_DELAY = 0.5
SPAWN_ORIGINAL_MAX_DELAY = 1.0

SPAWN_MIN_DELAY = SPAWN_ORIGINAL_MIN_DELAY
SPAWN_MAX_DELAY = SPAWN_ORIGINAL_MAX_DELAY

# The attack starts at BASE_HEAT_TIME just beyond the disabled angle and
# becomes faster as the red face approaches a horizontal/edge-on direction.
BASE_HEAT_TIME = 1.0
MIN_HEAT_TIME = 0.25
COOL_TIME = 0.7

# When the red face points within this angle of the viewer, the attack is
# completely disabled because it is pointing out of the screen rather than
# toward cubes in the surrounding 3D space.
TARGET_DISABLED_TILT_DEGREES = 20.0

# Length of each screen-space ray extending from a red-face corner along the
# matching projected depth edge of the cube.
TARGET_EDGE_RAY_LENGTH = math.hypot(WIDTH, HEIGHT) * 2.0

# Appearance of the faint targeting range while right-click is held.
TARGET_RANGE_ALPHA = 34
TARGET_RANGE_OUTLINE_ALPHA = 90

# MARK: - Game state / death & respawn tuning

STATE_PLAYING = "playing"
STATE_DYING = "dying"
STATE_GAMEOVER = "gameover"
STATE_RESPAWNING = "respawning"

game_state = STATE_PLAYING
state_timer = 0.0

DEATH_DURATION = 1.15
RESPAWN_DURATION = 0.7
FLASH_FADE_SPEED = 900.0  # alpha units per second
GAMEOVER_FADE_DURATION = 0.45  # how long the game-over text takes to ease in

STARTING_HEALTH = 10

cube_scale = 1.0
flash_alpha = 0.0
shake_magnitude = 0.0

# Extra spin applied on top of the normal mouse-driven yaw/pitch. This is
# what makes the cube tumble while it dies and while it materialises back
# in. It is always applied (even during normal play, where it just sits at
# 0.0) so that there is never a frame where this rotation is suddenly
# added or removed -- that abrupt add/remove is what caused the cube to
# visibly "snap" to a new orientation right as a respawn finished.
spin_offset_angle = 0.0
respawn_spin_start = 0.0


def smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def format_score(value):
    """Scores are always displayed as whole points."""
    return str(int(round(value)))


def get_mouse_facing_angles(mouse_x, mouse_y):
    """Return yaw/pitch that aim the front-face normal at the cursor.

    The mouse is treated as a point on the screen plane, with the cube
    CUBEDISTANCE units behind it.  Pitch must include the horizontal distance
    too; calculating it from vertical distance alone causes the face to drift
    when the cursor is near the left or right edge of the screen.
    """
    offset_x = mouse_x - WIDTH / 2
    offset_y = mouse_y - HEIGHT / 2

    yaw = -math.atan2(offset_x, CUBEDISTANCE)
    pitch = -math.atan2(
        offset_y,
        math.hypot(CUBEDISTANCE, offset_x)
    )

    return yaw, pitch


def get_player_colour(left_pressed, right_pressed):
    if right_pressed and not left_pressed:
        return PLAYER_RED

    return PLAYER_CYAN


def scale_colour(colour, amount):
    return tuple(
        max(0, min(255, int(component * amount)))
        for component in colour
    )


def render_tracked_text(text, text_font, colour, tracking):
    """Render compact display text with real letter spacing."""
    glyphs = [
        text_font.render(character, True, colour)
        for character in text
    ]

    width = sum(glyph.get_width() for glyph in glyphs)
    width += max(0, len(glyphs) - 1) * tracking
    height = max(glyph.get_height() for glyph in glyphs)

    surface = pygame.Surface((width, height), pygame.SRCALPHA)
    x = 0

    for glyph in glyphs:
        surface.blit(glyph, (x, 0))
        x += glyph.get_width() + tracking

    return surface


vertices = [
    [-1, -1, -1],
    [1, -1, -1],
    [1, 1, -1],
    [-1, 1, -1],
    [-1, -1, 1],
    [1, -1, 1],
    [1, 1, 1],
    [-1, 1, 1]
]

edges = [
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7)
]

faces = [
    (0, 1, 2, 3),  # initially facing the camera
    (4, 5, 6, 7),
    (0, 1, 5, 4),
    (2, 3, 7, 6),
    (1, 2, 6, 5),
    (0, 3, 7, 4)
]

COLOURED_FACE = faces[0]

# Each pair is (red-face corner, matching corner on the opposite face).
# Extending from the opposite corner through the red corner makes the
# targeting beam follow the cube's projected depth edges.
COLOURED_FACE_DEPTH_EDGES = (
    (0, 4),
    (1, 5),
    (2, 6),
    (3, 7)
)


def rotate_x(point, angle):
    x, y, z = point
    c = math.cos(angle)
    s = math.sin(angle)

    return [
        x,
        y * c - z * s,
        y * s + z * c
    ]


def rotate_y(point, angle):
    x, y, z = point
    c = math.cos(angle)
    s = math.sin(angle)

    return [
        x * c + z * s,
        y,
        -x * s + z * c
    ]


def rotate_z(point, angle):
    x, y, z = point
    c = math.cos(angle)
    s = math.sin(angle)

    return [
        x * c - y * s,
        x * s + y * c,
        z
    ]


def rotate_point(point, angle_x, angle_y, angle_z):
    point = rotate_x(point, angle_x)
    point = rotate_y(point, angle_y)
    point = rotate_z(point, angle_z)

    return point


def project_float(point, scale=1.0):
    x, y, z = point

    camera_distance = 5
    z += camera_distance

    # Avoid an unstable division if a projected helper point gets too close
    # to the camera plane.
    if abs(z) < 0.001:
        z = 0.001 if z >= 0 else -0.001

    factor = (CUBEDISTANCE * scale) / z

    return (
        WIDTH / 2 + x * factor,
        HEIGHT / 2 - y * factor
    )


def project(point, scale=1.0):
    screen_x, screen_y = project_float(point, scale)
    return int(screen_x), int(screen_y)


# Return the cross product of three 2D points
def cross_product(origin, point_a, point_b):
    return (
        (point_a[0] - origin[0])
        * (point_b[1] - origin[1])
        -
        (point_a[1] - origin[1])
        * (point_b[0] - origin[0])
    )


# Find the visible outer outline of the central cube
def convex_hull(points):
    points = sorted(set(points))

    if len(points) <= 1:
        return points

    lower = []

    for point in points:
        while (
            len(lower) >= 2
            and cross_product(
                lower[-2],
                lower[-1],
                point
            ) <= 0
        ):
            lower.pop()

        lower.append(point)

    upper = []

    for point in reversed(points):
        while (
            len(upper) >= 2
            and cross_product(
                upper[-2],
                upper[-1],
                point
            ) <= 0
        ):
            upper.pop()

        upper.append(point)

    return lower[:-1] + upper[:-1]


def point_inside_convex_polygon(point, polygon):
    if len(polygon) < 3:
        return False

    for index in range(len(polygon)):
        start = polygon[index]
        end = polygon[(index + 1) % len(polygon)]

        if cross_product(start, end, point) < -0.001:
            return False

    return True


def get_heat_time_for_tilt(tilt_degrees):
    # Convert the active tilt range from 20..90 degrees into 0..1.
    horizontal_amount = (
        tilt_degrees - TARGET_DISABLED_TILT_DEGREES
    ) / (90.0 - TARGET_DISABLED_TILT_DEGREES)

    horizontal_amount = max(
        0.0,
        min(1.0, horizontal_amount)
    )

    # At the activation boundary, use BASE_HEAT_TIME. As the face becomes
    # more horizontal/edge-on, smoothly approach MIN_HEAT_TIME.
    return (
        BASE_HEAT_TIME
        + (MIN_HEAT_TIME - BASE_HEAT_TIME)
        * horizontal_amount
    )


def get_red_face_targeting_region(rotated, projected):
    # The coloured face starts on z = -1, so its centre also gives the
    # rotated outward normal direction.
    face_center = [
        sum(rotated[index][axis] for index in COLOURED_FACE)
        / len(COLOURED_FACE)
        for axis in range(3)
    ]

    normal_length = math.sqrt(
        face_center[0] ** 2
        + face_center[1] ** 2
        + face_center[2] ** 2
    )

    if normal_length == 0:
        normal = (0.0, 0.0, -1.0)
    else:
        normal = tuple(
            component / normal_length
            for component in face_center
        )

    # At zero rotation normal.z is -1. A small tilt means the face points
    # almost directly out of the screen, where no useful 2D direction exists.
    facing_amount = max(-1.0, min(1.0, -normal[2]))
    tilt_degrees = math.degrees(math.acos(facing_amount))

    targeting_enabled = (
        tilt_degrees > TARGET_DISABLED_TILT_DEGREES
    )

    if not targeting_enabled:
        return [], False, tilt_degrees

    face_points = [
        projected[index]
        for index in COLOURED_FACE
    ]

    # Give every red-face corner its own direction. The direction runs from
    # the matching back-face corner through the red-face corner, so every ray
    # is the exact screen-space continuation of one of the cube's depth edges.
    extended_points = []

    for front_index, back_index in COLOURED_FACE_DEPTH_EDGES:
        front_point = projected[front_index]
        back_point = projected[back_index]

        direction_x = front_point[0] - back_point[0]
        direction_y = front_point[1] - back_point[1]
        direction_length = math.hypot(direction_x, direction_y)

        # This is very unlikely outside the direct-facing dead-zone, but avoid
        # dividing by zero if an edge happens to project to a single pixel.
        if direction_length < 0.001:
            continue

        direction_x /= direction_length
        direction_y /= direction_length

        extended_points.append((
            front_point[0] + direction_x * TARGET_EDGE_RAY_LENGTH,
            front_point[1] + direction_y * TARGET_EDGE_RAY_LENGTH
        ))

    if not extended_points:
        return [], False, tilt_degrees

    # The hull joins the red face and the four edge-aligned far points into
    # one convex perspective targeting region.
    targeting_polygon = convex_hull(
        face_points + extended_points
    )

    return targeting_polygon, True, tilt_degrees


def polygons_intersect(polygon_a, polygon_b):
    # Separating Axis Theorem for two convex screen-space polygons.
    for polygon in (polygon_a, polygon_b):
        for index in range(len(polygon)):
            start = polygon[index]
            end = polygon[(index + 1) % len(polygon)]

            edge_x = end[0] - start[0]
            edge_y = end[1] - start[1]
            axis_x = -edge_y
            axis_y = edge_x

            projections_a = [
                point[0] * axis_x + point[1] * axis_y
                for point in polygon_a
            ]
            projections_b = [
                point[0] * axis_x + point[1] * axis_y
                for point in polygon_b
            ]

            if (
                max(projections_a) < min(projections_b)
                or max(projections_b) < min(projections_a)
            ):
                return False

    return True


def draw_targeting_region(polygon):
    if len(polygon) < 3:
        return

    overlay = pygame.Surface(
        (WIDTH, HEIGHT),
        pygame.SRCALPHA
    )

    pygame.draw.polygon(
        overlay,
        (255, 45, 45, TARGET_RANGE_ALPHA),
        polygon
    )
    pygame.draw.polygon(
        overlay,
        (255, 80, 80, TARGET_RANGE_OUTLINE_ALPHA),
        polygon,
        2
    )

    screen.blit(overlay, (0, 0))


def draw_game_over_screen(elapsed, final_score, death_colour):
    """Draw a fractured neon end screen without placing UI in a panel."""
    fade_in = smoothstep(elapsed / GAMEOVER_FADE_DURATION)
    centre_x = WIDTH // 2
    centre_y = HEIGHT // 2 + 5

    # First turn the old playfield into a very faint scan-lined afterimage.
    atmosphere = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    atmosphere.fill((0, 0, 0, int(215 * fade_in)))

    for scan_y in range(0, HEIGHT, 6):
        pygame.draw.line(
            atmosphere,
            (255, 255, 255, int(7 * fade_in)),
            (0, scan_y),
            (WIDTH, scan_y)
        )

    # Rare, deterministic glitch bars keep the screen alive without making
    # the typography jitter constantly.
    glitch = max(0.0, math.sin(elapsed * 8.5)) ** 18

    if glitch > 0.05:
        glitch_y = int(
            centre_y + math.sin(elapsed * 29.0) * 120
        )
        pygame.draw.rect(
            atmosphere,
            (*GAMEOVER_RED, int(55 * fade_in * glitch)),
            (0, glitch_y, WIDTH, 3)
        )
        pygame.draw.rect(
            atmosphere,
            (*death_colour, int(38 * fade_in * glitch)),
            (0, glitch_y + 5, WIDTH, 1)
        )

    screen.blit(atmosphere, (0, 0))

    geometry = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    ring_rotation = elapsed * 0.28

    # A broken reticle takes the place of a conventional rectangular modal.
    # It reads as the ghost of the cube's targeting system after the cube has
    # shattered.
    outer_rect = pygame.Rect(
        centre_x - 158,
        centre_y - 158,
        316,
        316
    )
    inner_rect = pygame.Rect(
        centre_x - 136,
        centre_y - 136,
        272,
        272
    )

    arc_specs = (
        (0.08, 0.80, GAMEOVER_RED, 3),
        (1.06, 1.74, death_colour, 1),
        (2.08, 2.92, GAMEOVER_RED, 2),
        (3.37, 4.14, death_colour, 2),
        (4.56, 5.36, GAMEOVER_RED, 1),
        (5.72, 6.18, death_colour, 3),
    )

    for start_angle, end_angle, colour, width in arc_specs:
        pygame.draw.arc(
            geometry,
            (*colour, int(85 * fade_in)),
            outer_rect,
            start_angle + ring_rotation,
            end_angle + ring_rotation,
            width
        )

        pygame.draw.arc(
            geometry,
            (*colour, int(28 * fade_in)),
            inner_rect,
            end_angle - ring_rotation * 0.7,
            end_angle + 0.32 - ring_rotation * 0.7,
            1
        )

    # Uneven calibration ticks make the circle feel damaged rather than like
    # a clean loading spinner.
    for index in range(24):
        if index % 4 == 1:
            continue

        angle = math.tau * index / 24 - ring_rotation * 0.55
        tick_length = 13 if index % 3 == 0 else 7
        inner_radius = 166
        outer_radius = inner_radius + tick_length
        colour = GAMEOVER_RED if index % 2 == 0 else death_colour

        pygame.draw.line(
            geometry,
            (*colour, int(72 * fade_in)),
            (
                centre_x + math.cos(angle) * inner_radius,
                centre_y + math.sin(angle) * inner_radius
            ),
            (
                centre_x + math.cos(angle) * outer_radius,
                centre_y + math.sin(angle) * outer_radius
            ),
            1
        )

    # Fixed fracture paths reach beyond the reticle and deliberately break
    # the symmetry of the composition.
    fracture_paths = (
        ((-145, -57), (-210, -88), (-286, -74), (-365, -118)),
        ((136, -76), (205, -118), (270, -103), (360, -151)),
        ((157, 42), (226, 73), (294, 57), (375, 102)),
        ((-132, 91), (-196, 137), (-269, 126), (-347, 173)),
    )

    for path_index, relative_path in enumerate(fracture_paths):
        points = [
            (centre_x + point_x, centre_y + point_y)
            for point_x, point_y in relative_path
        ]
        colour = GAMEOVER_RED if path_index % 2 == 0 else death_colour

        pygame.draw.lines(
            geometry,
            (*colour, int(38 * fade_in)),
            False,
            points,
            1
        )

        branch_start = points[1]
        branch_direction = -1 if path_index % 2 == 0 else 1
        pygame.draw.line(
            geometry,
            (*colour, int(24 * fade_in)),
            branch_start,
            (
                branch_start[0] + 24 * branch_direction,
                branch_start[1] + 31
            ),
            1
        )

    # A slim interrupted horizon gives the score a baseline without putting
    # it inside a card or box.
    horizon_y = centre_y + 87
    horizon_reach = int(255 * fade_in)
    pygame.draw.line(
        geometry,
        (*GAMEOVER_RED, int(90 * fade_in)),
        (centre_x - horizon_reach, horizon_y),
        (centre_x - 72, horizon_y),
        1
    )
    pygame.draw.line(
        geometry,
        (*death_colour, int(90 * fade_in)),
        (centre_x + 72, horizon_y),
        (centre_x + horizon_reach, horizon_y),
        1
    )

    screen.blit(geometry, (0, 0))

    # Editorial-style typography: the title and score float directly in the
    # broken geometry rather than sitting in a menu rectangle.
    kicker = render_tracked_text(
        "// CORE SIGNAL LOST",
        GAMEOVER_KICKER_FONT,
        GAMEOVER_RED,
        3
    )
    kicker.set_alpha(int(230 * fade_in))
    kicker_rect = kicker.get_rect(
        center=(centre_x, 92 - 8 * (1.0 - fade_in))
    )
    screen.blit(kicker, kicker_rect)

    game_text = GAMEOVER_TITLE_FONT.render(
        "GAME", True, (242, 245, 248)
    )
    divider_text = GAMEOVER_TITLE_FONT.render(
        "/", True, death_colour
    )
    over_text = GAMEOVER_TITLE_FONT.render(
        "OVER", True, GAMEOVER_RED
    )

    for title_surface in (game_text, divider_text, over_text):
        title_surface.set_alpha(int(255 * fade_in))

    title_gap = 13
    title_width = (
        game_text.get_width()
        + divider_text.get_width()
        + over_text.get_width()
        + title_gap * 2
    )
    title_x = centre_x - title_width / 2
    title_y = 119 - 12 * (1.0 - fade_in)

    screen.blit(game_text, (title_x, title_y))
    title_x += game_text.get_width() + title_gap
    screen.blit(divider_text, (title_x, title_y))
    title_x += divider_text.get_width() + title_gap
    screen.blit(over_text, (title_x, title_y))

    score_label = render_tracked_text(
        "FINAL SCORE",
        GAMEOVER_LABEL_FONT,
        (175, 183, 192),
        4
    )
    score_label.set_alpha(int(220 * fade_in))
    score_label_rect = score_label.get_rect(
        center=(centre_x, centre_y - 74)
    )
    screen.blit(score_label, score_label_rect)

    score_string = format_score(final_score)
    score_main = GAMEOVER_SCORE_FONT.render(
        score_string, True, (248, 250, 252)
    )
    score_red = GAMEOVER_SCORE_FONT.render(
        score_string, True, GAMEOVER_RED
    )
    score_echo = GAMEOVER_SCORE_FONT.render(
        score_string, True, death_colour
    )

    chromatic_shift = 2 + int(5 * glitch)
    score_red.set_alpha(int(90 * fade_in))
    score_echo.set_alpha(int(70 * fade_in))
    score_main.set_alpha(int(255 * fade_in))

    score_rect = score_main.get_rect(
        center=(centre_x, centre_y + 3)
    )
    screen.blit(
        score_echo,
        (score_rect.x - chromatic_shift, score_rect.y + 1)
    )
    screen.blit(
        score_red,
        (score_rect.x + chromatic_shift, score_rect.y - 1)
    )
    screen.blit(score_main, score_rect)

    prompt_pulse = 0.62 + 0.38 * (
        (math.sin(elapsed * 3.4) + 1.0) / 2.0
    )
    prompt = render_tracked_text(
        "[ SPACE ]  RECONSTRUCT",
        GAMEOVER_PROMPT_FONT,
        PLAYER_CYAN,
        2
    )
    prompt.set_alpha(int(255 * fade_in * prompt_pulse))
    prompt_rect = prompt.get_rect(center=(centre_x, HEIGHT - 83))
    screen.blit(prompt, prompt_rect)

    prompt_line_length = int(112 * fade_in)
    pygame.draw.line(
        screen,
        scale_colour(PLAYER_CYAN, prompt_pulse),
        (prompt_rect.left - 18 - prompt_line_length, prompt_rect.centery),
        (prompt_rect.left - 18, prompt_rect.centery),
        1
    )
    pygame.draw.line(
        screen,
        scale_colour(PLAYER_CYAN, prompt_pulse),
        (prompt_rect.right + 18, prompt_rect.centery),
        (prompt_rect.right + 18 + prompt_line_length, prompt_rect.centery),
        1
    )

    left_meta = GAMEOVER_META_FONT.render(
        "RUN // TERMINATED", True, (95, 101, 110)
    )
    right_meta = GAMEOVER_META_FONT.render(
        "CORE // 00 HP", True, (95, 101, 110)
    )
    left_meta.set_alpha(int(210 * fade_in))
    right_meta.set_alpha(int(210 * fade_in))
    screen.blit(left_meta, (24, HEIGHT - 28))
    screen.blit(
        right_meta,
        (WIDTH - right_meta.get_width() - 24, HEIGHT - 28)
    )


class SmallCube:
    def __init__(self):
        margin = 30
        screen_edge = random.randrange(4)

        if screen_edge == 0:
            self.x = random.uniform(0, WIDTH)
            self.y = -margin

        elif screen_edge == 1:
            self.x = WIDTH + margin
            self.y = random.uniform(0, HEIGHT)

        elif screen_edge == 2:
            self.x = random.uniform(0, WIDTH)
            self.y = HEIGHT + margin

        else:
            self.x = -margin
            self.y = random.uniform(0, HEIGHT)

        # Move approximately towards the player
        target_x = WIDTH / 2 + random.uniform(-35, 35)
        target_y = HEIGHT / 2 + random.uniform(-35, 35)

        dx = target_x - self.x
        dy = target_y - self.y
        distance = math.hypot(dx, dy)

        speed = random.uniform(70, 120)

        self.vx = dx / distance * speed
        self.vy = dy / distance * speed

        self.size = random.uniform(10, 17)

        self.angle_x = random.uniform(0, math.tau)
        self.angle_y = random.uniform(0, math.tau)
        self.angle_z = random.uniform(0, math.tau)

        self.rotation_x = (
            random.choice((-1, 1))
            * random.uniform(0.7, 2.0)
        )

        self.rotation_y = (
            random.choice((-1, 1))
            * random.uniform(0.7, 2.0)
        )

        self.rotation_z = (
            random.choice((-1, 1))
            * random.uniform(0.4, 1.5)
        )

        # 0 = white and 1 = bright red
        self.heat = 0.0

    def update(self, dt, being_heated, heat_time):
        self.x += self.vx * dt
        self.y += self.vy * dt

        self.angle_x += self.rotation_x * dt
        self.angle_y += self.rotation_y * dt
        self.angle_z += self.rotation_z * dt

        if being_heated:
            self.heat += dt / heat_time
        else:
            self.heat -= dt / COOL_TIME

        self.heat = max(
            0.0,
            min(1.0, self.heat)
        )

    def get_projected_points(self):
        rotated_points = []
        projected_points = []

        for vertex in vertices:
            point = rotate_point(
                vertex,
                self.angle_x,
                self.angle_y,
                self.angle_z
            )

            rotated_points.append(point)

            x, y, z = point

            distance = 4
            factor = self.size * 3 / (z + distance)

            screen_x = self.x + x * factor
            screen_y = self.y - y * factor

            projected_points.append(
                (int(screen_x), int(screen_y))
            )

        return rotated_points, projected_points

    def is_fully_inside(self, main_outline):
        _, small_projected = self.get_projected_points()

        # Every corner must be inside the player's outline
        return all(
            point_inside_convex_polygon(
                point,
                main_outline
            )
            for point in small_projected
        )

    def is_hit_by_targeting_region(
        self,
        targeting_polygon
    ):
        _, small_projected = self.get_projected_points()
        small_outline = convex_hull(small_projected)

        # Heat the cube when any part of its projected silhouette overlaps
        # the square-face beam.
        return polygons_intersect(
            targeting_polygon,
            small_outline
        )

    def get_edge_colour(self):
        # White becomes hot red as the targeting field heats the cube.
        green_blue = int(
            255 * (1.0 - self.heat)
        )

        return (
            255,
            green_blue,
            green_blue
        )

    def draw(self):
        rotated_points, projected_points = (
            self.get_projected_points()
        )

        edge_colour = self.get_edge_colour()

        face_colour = tuple(
            int(component * 0.35)
            for component in edge_colour
        )

        face_list = []

        for face in faces:
            average_z = sum(
                rotated_points[index][2]
                for index in face
            ) / len(face)

            points = [
                projected_points[index]
                for index in face
            ]

            face_list.append(
                (average_z, points)
            )

        face_list.sort(
            key=lambda item: item[0],
            reverse=True
        )

        for _, points in face_list:
            pygame.draw.polygon(
                screen,
                face_colour,
                points
            )

        for start, end in edges:
            pygame.draw.line(
                screen,
                edge_colour,
                projected_points[start],
                projected_points[end],
                2
            )

    def is_offscreen(self):
        margin = 100

        return (
            self.x < -margin
            or self.x > WIDTH + margin
            or self.y < -margin
            or self.y > HEIGHT + margin
        )


class ExplosionParticle:
    def __init__(self, x, y):
        angle = random.uniform(0, math.tau)
        speed = random.uniform(80, 230)

        self.x = x
        self.y = y

        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed

        self.life = random.uniform(0.35, 0.75)
        self.maximum_life = self.life
        self.size = random.randint(2, 4)

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt

        drag = 0.97 ** (dt * 60)

        self.vx *= drag
        self.vy *= drag

        self.life -= dt

        return self.life > 0

    def draw(self):
        brightness = (
            self.life / self.maximum_life
        )

        colour = (
            int(255 * brightness),
            int(90 * brightness),
            int(25 * brightness)
        )

        pygame.draw.circle(
            screen,
            colour,
            (int(self.x), int(self.y)),
            max(
                1,
                int(self.size * brightness)
            )
        )


def create_explosion(x, y):
    return [
        ExplosionParticle(x, y)
        for _ in range(24)
    ]


class ImpactParticle:
    def __init__(
        self,
        x,
        y,
        colour,
        incoming_vx,
        incoming_vy
    ):
        incoming_angle = math.atan2(incoming_vy, incoming_vx)

        if random.random() < 0.72:
            angle = incoming_angle + random.uniform(-1.15, 1.15)
        else:
            angle = random.uniform(0, math.tau)

        speed = random.uniform(95, 285)

        self.x = x
        self.y = y
        self.vx = math.cos(angle) * speed + incoming_vx * 0.18
        self.vy = math.sin(angle) * speed + incoming_vy * 0.18

        self.life = random.uniform(0.24, 0.58)
        self.maximum_life = self.life
        self.length = random.uniform(3, 11)
        self.width = random.choice((1, 1, 2))
        self.colour = colour

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt

        drag = 0.95 ** (dt * 60)
        self.vx *= drag
        self.vy *= drag

        self.life -= dt
        return self.life > 0

    def draw(self):
        brightness = max(0.0, self.life / self.maximum_life)
        speed = math.hypot(self.vx, self.vy)

        if speed < 0.001:
            direction_x = 1.0
            direction_y = 0.0
        else:
            direction_x = self.vx / speed
            direction_y = self.vy / speed

        visible_length = self.length * brightness
        colour = scale_colour(self.colour, brightness)

        pygame.draw.line(
            screen,
            colour,
            (
                self.x - direction_x * visible_length,
                self.y - direction_y * visible_length
            ),
            (self.x, self.y),
            self.width
        )


class ImpactRing:
    def __init__(self, x, y, colour):
        self.x = x
        self.y = y
        self.colour = colour
        self.radius = 7.0
        self.expansion_speed = 125.0
        self.life = 0.34
        self.maximum_life = self.life

    def update(self, dt):
        self.radius += self.expansion_speed * dt
        self.expansion_speed *= 0.96 ** (dt * 60)
        self.life -= dt
        return self.life > 0

    def draw(self):
        brightness = max(0.0, self.life / self.maximum_life)
        pygame.draw.circle(
            screen,
            scale_colour(self.colour, brightness),
            (int(self.x), int(self.y)),
            max(1, int(self.radius)),
            1
        )


def create_impact_burst(cube, player_colour):
    cube_colour = cube.get_edge_colour()
    impact_particles = []

    for index in range(28):
        # Most fragments retain the incoming cube's current heat colour;
        # a few contact sparks borrow the player's colour at the collision.
        colour = cube_colour if index % 5 else player_colour
        impact_particles.append(
            ImpactParticle(
                cube.x,
                cube.y,
                colour,
                cube.vx,
                cube.vy
            )
        )

    impact_particles.append(
        ImpactRing(cube.x, cube.y, player_colour)
    )
    return impact_particles


# MARK: - Player death shatter effect
#
# The fatal-frame player colour is captured and passed into every shard, so a
# cyan cube cannot suddenly burst into red/white pieces (or vice versa).


class ShardParticle:
    def __init__(self, x, y, colour):
        angle = random.uniform(0, math.tau)
        speed = random.uniform(90, 420)

        self.x = x
        self.y = y

        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed

        self.spin = random.uniform(-12.0, 12.0)
        self.rotation = random.uniform(0, math.tau)

        self.life = random.uniform(0.6, 1.2)
        self.maximum_life = self.life
        self.length = random.uniform(6, 16)
        self.colour = colour
        self.intensity = random.uniform(0.72, 1.0)

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt

        drag = 0.985 ** (dt * 60)
        self.vx *= drag
        self.vy *= drag

        self.rotation += self.spin * dt
        self.life -= dt

        return self.life > 0

    def draw(self):
        brightness = self.life / self.maximum_life

        colour = scale_colour(
            self.colour,
            brightness * self.intensity
        )

        half_length = self.length * brightness / 2
        dx = math.cos(self.rotation) * half_length
        dy = math.sin(self.rotation) * half_length

        pygame.draw.line(
            screen,
            colour,
            (self.x - dx, self.y - dy),
            (self.x + dx, self.y + dy),
            2
        )


def create_death_shatter(x, y, colour):
    return [
        ShardParticle(x, y, colour)
        for _ in range(70)
    ]


small_cubes = []
particles = []
shatter_particles = []
player_death_colour = PLAYER_CYAN

spawn_timer = 0.5
running = True

current_mx, current_my = pygame.mouse.get_pos()
mx = float(current_mx)
my = float(current_my)
AIM_RESPONSE = 21.5

while running:
    dt = clock.tick(60) / 1000

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        elif event.type == pygame.KEYDOWN:
            if (
                event.key == pygame.K_SPACE
                and game_state == STATE_GAMEOVER
            ):
                # MARK: - Respawn
                # Reset all run state and play the materialise-in
                # animation before handing control back to the player.
                score = 0
                health = STARTING_HEALTH
                absorbing = False
                absorb_score_timer = 0.0

                small_cubes = []
                particles = []
                shatter_particles = []

                SPAWN_MIN_DELAY = SPAWN_ORIGINAL_MIN_DELAY
                SPAWN_MAX_DELAY = SPAWN_ORIGINAL_MAX_DELAY
                spawn_timer = 0.6

                cube_scale = 0.0
                flash_alpha = 0.0
                player_death_colour = PLAYER_CYAN

                # Ease the leftover tumble from the death animation back
                # down to exactly zero over the respawn, rather than
                # cutting it off, so there is no sudden re-orientation
                # once control returns to the player.
                respawn_spin_start = spin_offset_angle

                game_state = STATE_RESPAWNING
                state_timer = 0.0

    # MARK: - Death / respawn state machine

    shake_x = 0
    shake_y = 0

    if damage_shake_timer > 0:
        damage_shake_timer = max(0.0, damage_shake_timer - dt)

        progress = damage_shake_timer / DAMAGE_SHAKE_DURATION
        current_strength = DAMAGE_SHAKE_STRENGTH * progress

        shake_x = random.uniform(-current_strength, current_strength)
        shake_y = random.uniform(-current_strength, current_strength)

    if game_state == STATE_DYING:
        state_timer += dt
        cube_scale = max(0.0, 1.0 - state_timer / DEATH_DURATION)
        spin_offset_angle += dt * 7.0

        shake_progress = max(0.0, 1.0 - state_timer / DEATH_DURATION)
        shake_magnitude = 16.0 * shake_progress
        shake_x += random.uniform(-shake_magnitude, shake_magnitude)
        shake_y += random.uniform(-shake_magnitude, shake_magnitude)

        flash_alpha = max(0.0, flash_alpha - FLASH_FADE_SPEED * dt)

        if state_timer >= DEATH_DURATION:
            game_state = STATE_GAMEOVER
            state_timer = 0.0
            cube_scale = 0.0

    elif game_state == STATE_RESPAWNING:
        state_timer += dt
        respawn_progress = min(1.0, state_timer / RESPAWN_DURATION)
        eased_progress = smoothstep(respawn_progress)

        cube_scale = eased_progress
        # Interpolates from whatever spin the cube had left over from
        # dying down to precisely 0.0 by the time this state ends.
        spin_offset_angle = respawn_spin_start * (1.0 - eased_progress)

        if state_timer >= RESPAWN_DURATION:
            game_state = STATE_PLAYING
            cube_scale = 1.0
            spin_offset_angle = 0.0
            state_timer = 0.0

    elif game_state == STATE_GAMEOVER:
        state_timer += dt

    screen.fill((0, 0, 0))

    current_mx, current_my = pygame.mouse.get_pos()
    aim_blend = 1.0 - math.exp(-AIM_RESPONSE * dt)
    mx += (current_mx - mx) * aim_blend
    my += (current_my - my) * aim_blend

    left, middle, right = pygame.mouse.get_pressed()

    if game_state != STATE_PLAYING:
        # No attacking/shielding once the player has died or hasn't
        # respawned yet.
        left = False
        middle = False
        right = False

    absorbing = game_state == STATE_PLAYING and bool(left)

    yaw, pitch = get_mouse_facing_angles(mx, my)

    rotated = []
    projected = []

    for vertex in vertices:
        point = rotate_x(vertex, pitch)
        point = rotate_y(point, yaw)
        point = rotate_z(point, spin_offset_angle)

        rotated.append(point)
        projected.append(project(point, cube_scale))

    # Calculate the player's silhouette and square-face targeting beam
    # before they are used by the incoming-cube update below.
    main_outline = convex_hull(projected)

    (
        targeting_polygon,
        targeting_enabled,
        targeting_tilt_degrees
    ) = get_red_face_targeting_region(
        rotated,
        projected
    )

    targeting_active = (
        game_state == STATE_PLAYING
        and right
        and not left
        and targeting_enabled
    )

    current_heat_time = get_heat_time_for_tilt(
        targeting_tilt_degrees
    )

    if targeting_active:
        draw_targeting_region(targeting_polygon)

    # MARK: - Small cubes

    if game_state == STATE_PLAYING:
        # Spawn incoming cubes
        spawn_timer -= dt

        if spawn_timer <= 0:
            small_cubes.append(SmallCube())

            spawn_timer = random.uniform(
                SPAWN_MIN_DELAY,
                SPAWN_MAX_DELAY
            )

        surviving_cubes = []
        player_died_this_frame = False

        for cube in small_cubes:
            being_heated = (
                targeting_active
                and cube.is_hit_by_targeting_region(
                    targeting_polygon
                )
            )

            cube.update(
                dt,
                being_heated,
                current_heat_time
            )

            # Destroyed by the red field
            if cube.heat >= 1.0:
                particles.extend(
                    create_explosion(cube.x, cube.y)
                )
                score += 1
                continue

            # Only collide after the entire incoming cube
            # is enclosed by the player's outer outline.
            if cube.is_fully_inside(main_outline):
                particles.extend(
                    create_impact_burst(
                        cube,
                        get_player_colour(left, right)
                    )
                )

                if not absorbing:
                    print("You got hit")
                    damage_shake_timer = DAMAGE_SHAKE_DURATION
                    health -= 1

                    if health <= 0:
                        player_died_this_frame = True

                continue

            if not cube.is_offscreen():
                surviving_cubes.append(cube)

        if player_died_this_frame:
            # MARK: - Death
            # The player's cube shatters apart at the centre of the
            # screen; clear the incoming swarm so the moment reads
            # clearly instead of being buried under enemy cubes.
            small_cubes = []
            player_death_colour = get_player_colour(
                left,
                right
            )
            shatter_particles = create_death_shatter(
                WIDTH / 2,
                HEIGHT / 2,
                player_death_colour
            )

            game_state = STATE_DYING
            state_timer = 0.0
            spin_offset_angle = 0.0
            flash_alpha = 235.0
        else:
            small_cubes = surviving_cubes

    particles = [
        particle
        for particle in particles
        if particle.update(dt)
    ]

    shatter_particles = [
        particle
        for particle in shatter_particles
        if particle.update(dt)
    ]

    # MARK: - Center cube
    # Draw the player's wireframe over incoming cubes
    for start, end in edges:
        if game_state == STATE_PLAYING:
            color = get_player_colour(left, right)

        elif game_state == STATE_DYING:
            # Dim without changing hue, matching the colour captured by the
            # player's shatter particles on the fatal frame.
            color = scale_colour(
                player_death_colour,
                0.35 + 0.65 * cube_scale
            )

        elif game_state == STATE_RESPAWNING:
            # Materialises in from nothing, brightening to full cyan.
            color = (
                0,
                int(255 * cube_scale),
                int(255 * cube_scale)
            )

        else:
            color = (0, 0, 0)

        pygame.draw.line(
            screen,
            color,
            projected[start],
            projected[end],
            2
        )

    for particle in particles:
        particle.draw()

    for particle in shatter_particles:
        particle.draw()

    # Draw incoming cubes after the fill so they remain
    # visible until completely engulfed.
    for cube in small_cubes:
        cube.draw()

    if game_state == STATE_PLAYING:
        # Sort the central cube's faces
        face_list = []

        for face in faces:
            average_z = sum(
                rotated[index][2]
                for index in face
            ) / len(face)

            points = [
                projected[index]
                for index in face
            ]

            face_list.append(
                (average_z, points)
            )

        face_list.sort(
            key=lambda item: item[0],
            reverse=True
        )

        if left:
            # Left mouse: fill every face cyan
            for _, points in face_list:
                pygame.draw.polygon(
                    screen,
                    (60, 255, 255),
                    points
                )

        elif right:
            # Right mouse: fill the fixed physical face red
            points = [
                projected[index]
                for index in COLOURED_FACE
            ]

            pygame.draw.polygon(
                screen,
                (255, 30, 30),
                points
            )

        if absorbing:
            # Keep the score integer-only: one point every 1/7 second gives
            # the same seven-points-per-second rate at every frame rate.
            absorb_score_timer += dt
            elapsed_ticks = math.floor(
                (absorb_score_timer + 1e-12)
                / ABSORB_SCORE_TICK_INTERVAL
            )

            if elapsed_ticks > 0:
                score -= elapsed_ticks
                absorb_score_timer -= (
                    elapsed_ticks * ABSORB_SCORE_TICK_INTERVAL
                )
        else:
            absorb_score_timer = 0.0

        SPAWN_MIN_DELAY = max(
            SPAWN_ORIGINAL_MIN_DELAY - score * 0.002, 0
        )
        SPAWN_MAX_DELAY = max(
            SPAWN_ORIGINAL_MAX_DELAY - score * 0.002, 0
        )

    # MARK: - Death flash
    # A quick white flash at the instant of death, fading out over the
    # start of the death animation.
    if flash_alpha > 0:
        flash_overlay = pygame.Surface((WIDTH, HEIGHT))
        flash_overlay.fill((255, 255, 255))
        flash_overlay.set_alpha(int(flash_alpha))
        screen.blit(flash_overlay, (0, 0))

    if game_state != STATE_GAMEOVER:
        display_health = max(0, health)

        text_surface = HUD_FONT.render(
            format_score(score), True, (255, 255, 255)
        )
        screen.blit(text_surface, (10, 10))

        text_surface = HUD_FONT.render(
            str(display_health) + "hp", True, (255, 255, 255)
        )
        align_right_x = WIDTH - text_surface.get_width() - 10
        screen.blit(text_surface, (align_right_x, 10))

    # MARK: - Game over screen
    if game_state == STATE_GAMEOVER:
        draw_game_over_screen(
            state_timer,
            score,
            player_death_colour
        )

    # Composite the offscreen frame onto the real display surface,
    # applying the death screen-shake offset if any.
    display_surface.fill((0, 0, 0))
    display_surface.blit(screen, (shake_x, shake_y))
    pygame.display.flip()

pygame.quit()
