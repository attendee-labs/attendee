import logging
import math
import random
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PrimitiveMocapSequence:
    movements: list = field(default_factory=list)
    total_dx: int = 0
    total_dy: int = 0
    click_down_dt: float = 0.0
    click_up_dt: float = 0.0


class MocapManager:
    """
    Generates mouse movement primitives on demand instead of replaying captured
    files.

    The generated path uses a stochastic motor-control model:
      - movement duration follows a Fitts-law style distance/target-width rule;
      - the main path follows a minimum-jerk reaching trajectory, which gives the
        bell-shaped velocity profile typical of human pointing motions;
      - the cursor is simulated as a damped point mass tracking that motor plan;
      - lateral/parallel deviations are added as small curved sub-movements.

    This is intentionally not an anti-detection guarantee. It is a compact way to
    produce smooth, varied, human-like UI automation without storing mocap files.
    """

    _MIN_DT_SECONDS = 0.006
    _MAX_DT_SECONDS = 0.018

    def __init__(self, video_frame_size: tuple[int, int]):
        self.video_frame_size = video_frame_size
        self._initial_mouse_position = self._choose_initial_mouse_position()

    def get_initial_mouse_position(self) -> tuple[int, int] | None:
        return self._initial_mouse_position

    def find_random_sequence_landing_in_rect(
        self,
        current_x: int,
        current_y: int,
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> PrimitiveMocapSequence | None:
        return self._generate_sequence_landing_in_rect(
            current_x=current_x,
            current_y=current_y,
            rect_left=rect_left,
            rect_top=rect_top,
            rect_right=rect_right,
            rect_bottom=rect_bottom,
        )

    def find_random_sequence_landing_in_rect_with_stretch_and_rotation_allowed(
        self,
        current_x: int,
        current_y: int,
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> PrimitiveMocapSequence | None:
        # Kept for API compatibility with the old mocap-backed implementation.
        # There is no longer anything to stretch or rotate: we generate a fresh
        # physically-plausible motion directly toward the target rectangle.
        return self._generate_sequence_landing_in_rect(
            current_x=current_x,
            current_y=current_y,
            rect_left=rect_left,
            rect_top=rect_top,
            rect_right=rect_right,
            rect_bottom=rect_bottom,
        )

    def _generate_sequence_landing_in_rect(
        self,
        current_x: int,
        current_y: int,
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> PrimitiveMocapSequence | None:
        rect_left, rect_top, rect_right, rect_bottom = self._normalize_rect(
            rect_left,
            rect_top,
            rect_right,
            rect_bottom,
        )

        target_x, target_y = self._choose_target_point_in_rect(
            rect_left=rect_left,
            rect_top=rect_top,
            rect_right=rect_right,
            rect_bottom=rect_bottom,
        )

        sequence = self._generate_sequence_to_point(
            current_x=current_x,
            current_y=current_y,
            target_x=target_x,
            target_y=target_y,
            target_width=max(1, rect_right - rect_left + 1),
            target_height=max(1, rect_bottom - rect_top + 1),
        )

        final_x = current_x + sequence.total_dx
        final_y = current_y + sequence.total_dy
        if not (rect_left <= final_x <= rect_right and rect_top <= final_y <= rect_bottom):
            # This should be unreachable because the final rounded position is
            # forced to the selected target pixel, but keep the API honest.
            logger.warning(
                "Generated mouse sequence missed target rect: final=(%s, %s), rect=(%s, %s, %s, %s)",
                final_x,
                final_y,
                rect_left,
                rect_top,
                rect_right,
                rect_bottom,
            )
            return None

        logger.info(
            "Generated mouse sequence with %s movement events from (%s, %s) to (%s, %s)",
            len(sequence.movements),
            current_x,
            current_y,
            final_x,
            final_y,
        )
        return sequence

    def _generate_sequence_to_point(
        self,
        current_x: int,
        current_y: int,
        target_x: int,
        target_y: int,
        target_width: int,
        target_height: int,
    ) -> PrimitiveMocapSequence:
        target_dx = target_x - current_x
        target_dy = target_y - current_y
        distance = math.hypot(target_dx, target_dy)

        if distance < 0.5:
            return PrimitiveMocapSequence(
                movements=[],
                total_dx=0,
                total_dy=0,
                click_down_dt=random.uniform(0.06, 0.18),
                click_up_dt=random.uniform(0.035, 0.09),
            )

        target_size = max(1.0, min(target_width, target_height))
        duration = self._movement_duration_seconds(distance=distance, target_size=target_size)
        dts = self._sample_frame_dts(duration)

        positions, dts = self._sample_physical_trajectory(
            target_dx=float(target_dx),
            target_dy=float(target_dy),
            distance=distance,
            dts=dts,
        )

        movements: list[dict] = []
        prev_x = 0
        prev_y = 0
        total_dx = 0
        total_dy = 0
        pending_dt = 0.0

        max_x = max(0, self.video_frame_size[0] - 1)
        max_y = max(0, self.video_frame_size[1] - 1)

        for i, ((x, y), dt) in enumerate(zip(positions, dts)):
            pending_dt += dt

            if i == len(positions) - 1:
                rounded_x = target_dx
                rounded_y = target_dy
            else:
                absolute_x = self._clamp(round(current_x + x), 0, max_x)
                absolute_y = self._clamp(round(current_y + y), 0, max_y)
                rounded_x = absolute_x - current_x
                rounded_y = absolute_y - current_y

            dx = int(rounded_x - prev_x)
            dy = int(rounded_y - prev_y)
            if dx == 0 and dy == 0:
                continue

            prev_x += dx
            prev_y += dy
            total_dx += dx
            total_dy += dy

            movements.append(
                {
                    "type": "mouse_move",
                    "dt": pending_dt,
                    "dx": dx,
                    "dy": dy,
                    "global_x": current_x + prev_x,
                    "global_y": current_y + prev_y,
                }
            )
            pending_dt = 0.0

        if total_dx != target_dx or total_dy != target_dy:
            residual_dx = target_dx - total_dx
            residual_dy = target_dy - total_dy
            total_dx += residual_dx
            total_dy += residual_dy
            movements.append(
                {
                    "type": "mouse_move",
                    "dt": max(pending_dt, random.uniform(0.006, 0.014)),
                    "dx": residual_dx,
                    "dy": residual_dy,
                    "global_x": current_x + total_dx,
                    "global_y": current_y + total_dy,
                }
            )
            pending_dt = 0.0

        return PrimitiveMocapSequence(
            movements=movements,
            total_dx=total_dx,
            total_dy=total_dy,
            click_down_dt=pending_dt + self._settle_before_click_seconds(distance),
            click_up_dt=random.uniform(0.035, 0.095),
        )

    def _sample_physical_trajectory(
        self,
        target_dx: float,
        target_dy: float,
        distance: float,
        dts: list[float],
    ) -> tuple[list[tuple[float, float]], list[float]]:
        ux = target_dx / distance
        uy = target_dy / distance
        px = -uy
        py = ux

        mass = random.uniform(0.8, 1.25)
        natural_frequency = random.uniform(18.0, 28.0)
        damping_ratio = random.uniform(0.78, 1.02)
        stiffness = mass * natural_frequency**2
        damping = 2.0 * damping_ratio * mass * natural_frequency

        lateral_scale = min(22.0, max(0.6, distance * random.uniform(0.012, 0.035)))
        parallel_scale = min(7.0, max(0.25, distance * random.uniform(0.002, 0.008)))
        motor_noise = min(900.0, 35.0 + distance * random.uniform(0.10, 0.22))

        lateral_modes = [
            (random.uniform(-0.75, 0.75) * lateral_scale, random.choice((1, 2))),
            (random.uniform(-0.35, 0.35) * lateral_scale, random.choice((2, 3))),
        ]
        parallel_modes = [
            (random.uniform(-0.55, 0.55) * parallel_scale, random.choice((1, 2))),
            (random.uniform(-0.25, 0.25) * parallel_scale, random.choice((2, 3))),
        ]

        total = sum(dts)
        elapsed = 0.0
        x = 0.0
        y = 0.0
        vx = 0.0
        vy = 0.0
        prev_command_x = 0.0
        prev_command_y = 0.0
        positions: list[tuple[float, float]] = []

        for dt in dts:
            elapsed += dt
            u = 1.0 if total <= 0.0 else self._clamp(elapsed / total, 0.0, 1.0)
            command_x, command_y, envelope = self._motor_command_point(
                u=u,
                target_dx=target_dx,
                target_dy=target_dy,
                ux=ux,
                uy=uy,
                px=px,
                py=py,
                lateral_modes=lateral_modes,
                parallel_modes=parallel_modes,
            )

            command_vx = (command_x - prev_command_x) / dt
            command_vy = (command_y - prev_command_y) / dt
            prev_command_x = command_x
            prev_command_y = command_y

            noise_parallel = random.gauss(0.0, motor_noise * 0.35 * envelope)
            noise_lateral = random.gauss(0.0, motor_noise * envelope)

            ax = (stiffness * (command_x - x) + damping * (command_vx - vx)) / mass + ux * noise_parallel + px * noise_lateral
            ay = (stiffness * (command_y - y) + damping * (command_vy - vy)) / mass + uy * noise_parallel + py * noise_lateral

            vx += ax * dt
            vy += ay * dt
            x += vx * dt
            y += vy * dt
            positions.append((x, y))

        settle_time = 0.0
        while settle_time < 0.28:
            remaining = math.hypot(target_dx - x, target_dy - y)
            speed = math.hypot(vx, vy)
            if remaining <= 0.35 and speed <= 8.0:
                break

            dt = random.uniform(self._MIN_DT_SECONDS, self._MAX_DT_SECONDS)
            settle_time += dt
            dts.append(dt)

            ax = (stiffness * (target_dx - x) - damping * vx) / mass
            ay = (stiffness * (target_dy - y) - damping * vy) / mass
            vx += ax * dt
            vy += ay * dt
            x += vx * dt
            y += vy * dt
            positions.append((x, y))

        positions[-1] = (target_dx, target_dy)
        return positions, dts

    def _motor_command_point(
        self,
        u: float,
        target_dx: float,
        target_dy: float,
        ux: float,
        uy: float,
        px: float,
        py: float,
        lateral_modes: list[tuple[float, int]],
        parallel_modes: list[tuple[float, int]],
    ) -> tuple[float, float, float]:
        progress = self._minimum_jerk(u)
        envelope = math.sin(math.pi * u) ** 1.35

        lateral = 0.0
        for amplitude, harmonic in lateral_modes:
            lateral += amplitude * math.sin(harmonic * math.pi * u)

        parallel = 0.0
        for amplitude, harmonic in parallel_modes:
            parallel += amplitude * math.sin(harmonic * math.pi * u)

        lateral *= envelope
        parallel *= envelope

        x = target_dx * progress + ux * parallel + px * lateral
        y = target_dy * progress + uy * parallel + py * lateral
        return x, y, envelope

    def _movement_duration_seconds(self, distance: float, target_size: float) -> float:
        index_of_difficulty = math.log2(distance / target_size + 1.0)
        intercept = random.gauss(0.085, 0.018)
        slope = random.gauss(0.105, 0.018)
        duration = intercept + slope * index_of_difficulty + random.gauss(0.0, 0.025)

        lower = 0.11 if distance < 20.0 else 0.18
        upper = 0.75 if distance < 600.0 else 1.25
        return self._clamp(duration, lower, upper)

    def _sample_frame_dts(self, duration: float) -> list[float]:
        dts: list[float] = []
        remaining = duration

        while remaining > 0.0:
            dt = random.uniform(self._MIN_DT_SECONDS, self._MAX_DT_SECONDS)
            if remaining - dt < self._MIN_DT_SECONDS:
                dt = remaining
            dts.append(dt)
            remaining -= dt

        if len(dts) < 5:
            count = 5
            base = duration / count
            dts = [base for _ in range(count)]

        return dts

    def _settle_before_click_seconds(self, distance: float) -> float:
        base = 0.045 + min(0.08, distance / 6000.0)
        return self._clamp(random.gauss(base, 0.025), 0.025, 0.16)

    def _choose_target_point_in_rect(
        self,
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> tuple[int, int]:
        width = rect_right - rect_left + 1
        height = rect_bottom - rect_top + 1
        center_x = (rect_left + rect_right) / 2.0
        center_y = (rect_top + rect_bottom) / 2.0

        if width <= 2 and height <= 2:
            return round(center_x), round(center_y)

        # Aim near the center, not the exact center every time. The clamped
        # interior band avoids edge clicks unless the rect is very small.
        inset_x = min(width * 0.22, max(0.0, (width - 1) / 2.0))
        inset_y = min(height * 0.22, max(0.0, (height - 1) / 2.0))

        min_x = rect_left + inset_x
        max_x = rect_right - inset_x
        min_y = rect_top + inset_y
        max_y = rect_bottom - inset_y

        if min_x > max_x:
            min_x = max_x = center_x
        if min_y > max_y:
            min_y = max_y = center_y

        x = random.gauss(center_x, max(0.5, width / 7.0))
        y = random.gauss(center_y, max(0.5, height / 7.0))
        return (
            int(round(self._clamp(x, min_x, max_x))),
            int(round(self._clamp(y, min_y, max_y))),
        )

    def _choose_initial_mouse_position(self) -> tuple[int, int] | None:
        width, height = self.video_frame_size
        if width <= 0 or height <= 0:
            return None

        # Replaces the old value inferred from the first mocap file.
        x = random.gauss(width * 0.50, width * 0.08)
        y = random.gauss(height * 0.68, height * 0.08)
        return (
            int(round(self._clamp(x, 0, width - 1))),
            int(round(self._clamp(y, 0, height - 1))),
        )

    @staticmethod
    def _minimum_jerk(u: float) -> float:
        # 10u^3 - 15u^4 + 6u^5.
        # Starts and ends with zero velocity and acceleration.
        return 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5

    @staticmethod
    def _normalize_rect(
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> tuple[int, int, int, int]:
        left, right = sorted((rect_left, rect_right))
        top, bottom = sorted((rect_top, rect_bottom))
        return left, top, right, bottom

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return min(max(value, lower), upper)
