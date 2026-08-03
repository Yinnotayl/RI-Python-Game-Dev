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
font = pygame.font.Font(None, 25)

CUBEDISTANCE = 200

score = 0
health = 10

SCORE_DECREASE_DELAY = 60/5
score_decrease = SCORE_DECREASE_DELAY
absorbing = False

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

    def draw(self):
        rotated_points, projected_points = (
            self.get_projected_points()
        )

        # White to bright red
        green_blue = int(
            255 * (1.0 - self.heat)
        )

        edge_colour = (
            255,
            green_blue,
            green_blue
        )

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


# MARK: - Player death shatter effect
#
# When the player dies, the central cube blows apart into shards that carry
# its own colour scheme (cyan edges / red face) rather than the warm
# orange embers used for the small enemy cubes, so the moment reads as
# "the player's cube" breaking rather than another enemy dying.

DEATH_PALETTE = (
    (80, 255, 255),
    (255, 60, 60),
    (255, 255, 255),
)


class ShardParticle:
    def __init__(self, x, y):
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
        self.colour = random.choice(DEATH_PALETTE)

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

        colour = tuple(
            int(component * brightness)
            for component in self.colour
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


def create_death_shatter(x, y):
    return [
        ShardParticle(x, y)
        for _ in range(70)
    ]


small_cubes = []
particles = []
shatter_particles = []

spawn_timer = 0.5
running = True

current_mx = 0
current_my = 0
change_factor = 0.3
mx = 0
my = 0

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
                score_decrease = SCORE_DECREASE_DELAY
                absorbing = False

                small_cubes = []
                particles = []
                shatter_particles = []

                SPAWN_MIN_DELAY = SPAWN_ORIGINAL_MIN_DELAY
                SPAWN_MAX_DELAY = SPAWN_ORIGINAL_MAX_DELAY
                spawn_timer = 0.6

                cube_scale = 0.0
                flash_alpha = 0.0

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

    if game_state == STATE_DYING:
        state_timer += dt
        cube_scale = max(0.0, 1.0 - state_timer / DEATH_DURATION)
        spin_offset_angle += dt * 7.0

        shake_progress = max(0.0, 1.0 - state_timer / DEATH_DURATION)
        shake_magnitude = 16.0 * shake_progress
        shake_x = random.uniform(-shake_magnitude, shake_magnitude)
        shake_y = random.uniform(-shake_magnitude, shake_magnitude)

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
    mx += (current_mx - mx) * change_factor
    my += (current_my - my) * change_factor

    left, middle, right = pygame.mouse.get_pressed()

    if game_state != STATE_PLAYING:
        # No attacking/shielding once the player has died or hasn't
        # respawned yet.
        left = False
        middle = False
        right = False

    yaw = -math.atan(
        (mx - WIDTH / 2) / CUBEDISTANCE
    )

    pitch = -math.atan(
        (my - HEIGHT / 2) / CUBEDISTANCE
    )

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
                if not absorbing:
                    print("You got hit")
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
            shatter_particles = create_death_shatter(
                WIDTH / 2, HEIGHT / 2
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
            color = (0, 255, 255)

            if left:
                color = (0, 255, 255)
            elif right:
                color = (255, 0, 0)

        elif game_state == STATE_DYING:
            # Cools from white-hot to red as the cube breaks apart.
            fade = 1.0 - cube_scale
            color = (
                255,
                int(255 * (1.0 - fade)),
                int(255 * (1.0 - fade))
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

            absorbing = True
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

        if not left:
            absorbing = False

        if absorbing and score_decrease == 0:
            score -= 1
            score_decrease = SCORE_DECREASE_DELAY
        elif absorbing:
            score_decrease -= 1

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

    display_health = max(0, health)

    hud_score_font = pygame.font.SysFont('Cascadia Code', 30)
    text_surface = hud_score_font.render(
        str(score), True, (255, 255, 255)
    )
    screen.blit(text_surface, (10, 10))

    health_font = pygame.font.SysFont('Cascadia Code', 30)
    text_surface = health_font.render(
        str(display_health) + "hp", True, (255, 255, 255)
    )
    align_right_x = WIDTH - text_surface.get_width() - 10
    screen.blit(text_surface, (align_right_x, 10))

    # MARK: - Game over screen
    if game_state == STATE_GAMEOVER:
        # Ease everything in rather than having it pop onto the screen
        # the instant the death animation ends.
        fade_in = smoothstep(state_timer / GAMEOVER_FADE_DURATION)

        pulse = (math.sin(state_timer * 3.2) + 1) / 2  # 0..1

        dim_overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        dim_overlay.fill((0, 0, 0, int(165 * fade_in)))
        screen.blit(dim_overlay, (0, 0))

        title_font = pygame.font.SysFont(
            'Cascadia Code', 56, bold=False
        )

        # "GAME OVER" in red immediately followed by the score in white,
        # on one simple centred line.
        gameover_surface = title_font.render(
            "GAME OVER", True, (255, 0, 0)
        )
        score_surface = title_font.render(
            " " + str(score), True, (255, 255, 255)
        )

        gameover_surface.set_alpha(int(255 * fade_in))
        score_surface.set_alpha(int(255 * fade_in))

        total_width = (
            gameover_surface.get_width() + score_surface.get_width()
        )
        left_x = WIDTH / 2 - total_width / 2
        # A small settle-in from slightly above its resting spot.
        title_y = HEIGHT / 2 - 50 - 12 * (1.0 - fade_in)

        screen.blit(gameover_surface, (left_x, title_y))
        screen.blit(
            score_surface,
            (left_x + gameover_surface.get_width(), title_y)
        )

        # Left-aligned to the same left edge as the title above it,
        # rather than centred on its own.
        prompt_font = pygame.font.SysFont('Cascadia Code', 24)
        prompt_surface = prompt_font.render(
            "Press SPACE to Respawn", True, (0, 255, 255)
        )
        prompt_surface.set_alpha(int(fade_in * (140 + 115 * pulse)))
        prompt_y = title_y + gameover_surface.get_height() + 18
        screen.blit(prompt_surface, (left_x, prompt_y))

    # Composite the offscreen frame onto the real display surface,
    # applying the death screen-shake offset if any.
    display_surface.fill((0, 0, 0))
    display_surface.blit(screen, (shake_x, shake_y))
    pygame.display.flip()

pygame.quit()