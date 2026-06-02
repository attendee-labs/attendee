import glob
import json
import logging
import math
import os
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
    def __init__(self, video_frame_size: tuple[int, int]):
        self.video_frame_size = video_frame_size
        self.sequences: list[PrimitiveMocapSequence] = []
        self._initial_mouse_position: tuple[int, int] | None = None
        self._load_all_scramble_files()
        self._generate_perturbed_sequences()

    def _generate_perturbed_sequences(self):
        original_sequences = list(self.sequences)
        for seq in original_sequences:
            for i in range(40):
                angle = -5 + (10 / 41) * (i + 1)
                if angle == 0:
                    continue
                rad = math.radians(angle)
                cos_a = math.cos(rad)
                sin_a = math.sin(rad)

                perturbed_movements = []
                total_dx = 0
                total_dy = 0
                for mov in seq.movements:
                    dx = mov.get("dx", 0)
                    dy = mov.get("dy", 0)
                    new_dx = round(dx * cos_a - dy * sin_a)
                    new_dy = round(dx * sin_a + dy * cos_a)
                    total_dx += new_dx
                    total_dy += new_dy
                    perturbed_mov = dict(mov)
                    perturbed_mov["dx"] = new_dx
                    perturbed_mov["dy"] = new_dy
                    perturbed_movements.append(perturbed_mov)

                self.sequences.append(
                    PrimitiveMocapSequence(
                        movements=perturbed_movements,
                        total_dx=total_dx,
                        total_dy=total_dy,
                        click_down_dt=seq.click_down_dt,
                        click_up_dt=seq.click_up_dt,
                    )
                )

        logger.info(f"Generated {len(self.sequences) - len(original_sequences)} perturbed sequences ({len(self.sequences)} total)")

    def _load_all_scramble_files(self):
        directory = os.path.dirname(__file__)
        pattern = os.path.join(directory, "mocap", f"join_mocap_{self.video_frame_size[1]}p_*.json")
        file_paths = sorted(glob.glob(pattern))

        logger.info(f"Found {len(file_paths)} mocap scramble files")

        for file_path in file_paths:
            with open(file_path, "r") as f:
                events = json.load(f)
            if self._initial_mouse_position is None and events:
                first = events[0]
                self._initial_mouse_position = (
                    first["global_x"] - first.get("dx", 0),
                    first["global_y"] - first.get("dy", 0),
                )
            primitives = self._parse_primitives(events)
            logger.info(f"Parsed {len(primitives)} primitive sequences from {os.path.basename(file_path)}")
            self.sequences.extend(primitives)

    def _parse_primitives(self, events: list[dict]) -> list[PrimitiveMocapSequence]:
        primitives = []
        current_movements = []
        dx_acc = 0
        dy_acc = 0
        click_down_dt = 0.0

        for event in events:
            if event["type"] == "mouse_move":
                current_movements.append(event)
                dx_acc += event.get("dx", 0)
                dy_acc += event.get("dy", 0)

            elif event["type"] == "mouse_click" and event.get("state") == "down":
                click_down_dt = event.get("dt", 0.0)

            elif event["type"] == "mouse_click" and event.get("state") == "up":
                primitives.append(
                    PrimitiveMocapSequence(
                        movements=current_movements,
                        total_dx=dx_acc,
                        total_dy=dy_acc,
                        click_down_dt=click_down_dt,
                        click_up_dt=event.get("dt", 0.0),
                    )
                )
                current_movements = []
                dx_acc = 0
                dy_acc = 0
                click_down_dt = 0.0

        return primitives

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
        matching = [seq for seq in self.sequences if rect_left <= current_x + seq.total_dx <= rect_right and rect_top <= current_y + seq.total_dy <= rect_bottom]
        logger.info(f"Found {len(matching)} sequences matching the rect")
        if not matching:
            return None
        return random.choice(matching)

    def _sequence_lands_in_rect(
        self,
        seq: PrimitiveMocapSequence,
        current_x: int,
        current_y: int,
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> bool:
        final_x = current_x + seq.total_dx
        final_y = current_y + seq.total_dy
        return rect_left <= final_x <= rect_right and rect_top <= final_y <= rect_bottom

    def _transform_sequence(
        self,
        seq: PrimitiveMocapSequence,
        scale: float,
        rotation_degrees: float,
    ) -> PrimitiveMocapSequence:
        rad = math.radians(rotation_degrees)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)

        transformed_movements = []
        total_dx = 0
        total_dy = 0

        for mov in seq.movements:
            dx = mov.get("dx", 0)
            dy = mov.get("dy", 0)

            scaled_dx = dx * scale
            scaled_dy = dy * scale

            new_dx = round(scaled_dx * cos_a - scaled_dy * sin_a)
            new_dy = round(scaled_dx * sin_a + scaled_dy * cos_a)

            total_dx += new_dx
            total_dy += new_dy

            transformed_mov = dict(mov)
            transformed_mov["dx"] = new_dx
            transformed_mov["dy"] = new_dy
            transformed_movements.append(transformed_mov)

        return PrimitiveMocapSequence(
            movements=transformed_movements,
            total_dx=total_dx,
            total_dy=total_dy,
            click_down_dt=seq.click_down_dt,
            click_up_dt=seq.click_up_dt,
        )

    @staticmethod
    def _normalize_angle_degrees(angle: float) -> float:
        # Return an equivalent angle in [-180, 180).
        return (angle + 180.0) % 360.0 - 180.0

    @staticmethod
    def _ray_rect_radius_interval(
        angle_degrees: float,
        rect_left: float,
        rect_top: float,
        rect_right: float,
        rect_bottom: float,
    ) -> tuple[float, float] | None:
        """
        For the ray from (0, 0) at angle_degrees, return the inclusive interval of
        distances along the ray where the point is inside the axis-aligned rect.

        Rect coordinates are relative to the current mouse position, not absolute
        screen coordinates.
        """
        rad = math.radians(angle_degrees)
        dx = math.cos(rad)
        dy = math.sin(rad)

        t_min = 0.0
        t_max = math.inf
        eps = 1e-9

        for lower, upper, direction in (
            (rect_left, rect_right, dx),
            (rect_top, rect_bottom, dy),
        ):
            if abs(direction) < eps:
                # Ray is parallel to these slab boundaries. It either always
                # satisfies this axis or never can.
                if lower <= 0.0 <= upper:
                    continue
                return None

            a = lower / direction
            b = upper / direction
            if a > b:
                a, b = b, a

            t_min = max(t_min, a)
            t_max = min(t_max, b)

            if t_min > t_max:
                return None

        if t_max < 0.0:
            return None

        return max(t_min, 0.0), t_max

    def _candidate_transform_params_for_rect(
        self,
        seq: PrimitiveMocapSequence,
        rect_left: float,
        rect_top: float,
        rect_right: float,
        rect_bottom: float,
        scale_pct: float,
        max_rot_deg: float,
    ) -> list[tuple[float, float]]:
        """
        Compute viable (scale, rotation_degrees) pairs by reasoning about only the
        sequence endpoint vector.

        A transformed endpoint must be scale * R(theta) * (seq.total_dx,
        seq.total_dy). The reachable endpoints for a tier form an annular sector.
        This method intersects candidate rays in that sector with the target rect,
        then returns only params whose continuous endpoint can land inside it.
        """
        seq_len = math.hypot(seq.total_dx, seq.total_dy)
        if seq_len < 1e-9:
            return []

        base_angle = math.degrees(math.atan2(seq.total_dy, seq.total_dx))
        min_radius = seq_len * (1.0 - scale_pct)
        max_radius = seq_len * (1.0 + scale_pct)

        center_x = (rect_left + rect_right) / 2.0
        center_y = (rect_top + rect_bottom) / 2.0

        # These points are just used to propose angles. Ray/rect intersection below
        # verifies whether each proposed angle actually has a usable radius.
        probe_points = [
            (center_x, center_y),
            (rect_left, rect_top),
            (rect_left, rect_bottom),
            (rect_right, rect_top),
            (rect_right, rect_bottom),
            (center_x, rect_top),
            (center_x, rect_bottom),
            (rect_left, center_y),
            (rect_right, center_y),
        ]

        candidate_rotations = {-max_rot_deg, 0.0, max_rot_deg}
        for x, y in probe_points:
            if abs(x) < 1e-9 and abs(y) < 1e-9:
                continue
            target_angle = math.degrees(math.atan2(y, x))
            rotation = self._normalize_angle_degrees(target_angle - base_angle)
            rotation = min(max(rotation, -max_rot_deg), max_rot_deg)
            candidate_rotations.add(rotation)

        # Avoid bias from set iteration while preserving the randomized nature of
        # the previous implementation.
        candidate_rotations = list(candidate_rotations)
        random.shuffle(candidate_rotations)

        params: list[tuple[float, float]] = []
        seen: set[tuple[float, float]] = set()

        for rotation in candidate_rotations:
            endpoint_angle = base_angle + rotation
            ray_interval = self._ray_rect_radius_interval(
                endpoint_angle,
                rect_left,
                rect_top,
                rect_right,
                rect_bottom,
            )
            if ray_interval is None:
                continue

            ray_min, ray_max = ray_interval
            overlap_min = max(ray_min, min_radius)
            overlap_max = min(ray_max, max_radius)
            if overlap_min > overlap_max:
                continue

            # Aim for the middle of the overlap so integer rounding inside
            # _transform_sequence is less likely to push the endpoint out of rect.
            radius = (overlap_min + overlap_max) / 2.0
            scale = radius / seq_len

            key = (round(scale, 6), round(rotation, 6))
            if key in seen:
                continue
            seen.add(key)
            params.append((scale, rotation))

        return params

    # Fallback for when we absolutely need to find a motion sequence that lands in the rect, even if it means stretching or rotating it
    def find_random_sequence_landing_in_rect_with_stretch_and_rotation_allowed(
        self,
        current_x: int,
        current_y: int,
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> PrimitiveMocapSequence | None:
        tiers = [
            (0.02, 2),
            (0.04, 4),
            (0.06, 6),
            (0.08, 8),
            (0.10, 10),
            (0.15, 15),
        ]

        # Work in a coordinate system where the current mouse position is the
        # origin. The target rect then describes the allowed final delta vector.
        target_left = rect_left - current_x
        target_top = rect_top - current_y
        target_right = rect_right - current_x
        target_bottom = rect_bottom - current_y

        best_available: list[PrimitiveMocapSequence] = []

        for scale_pct, max_rot_deg in tiers:
            matching: list[PrimitiveMocapSequence] = []
            transformed_attempts = 0
            analytic_candidates = 0

            sequences = list(self.sequences)
            random.shuffle(sequences)

            for seq in sequences:
                params = self._candidate_transform_params_for_rect(
                    seq=seq,
                    rect_left=target_left,
                    rect_top=target_top,
                    rect_right=target_right,
                    rect_bottom=target_bottom,
                    scale_pct=scale_pct,
                    max_rot_deg=max_rot_deg,
                )
                analytic_candidates += len(params)

                for scale, angle in params:
                    transformed_attempts += 1
                    transformed = self._transform_sequence(
                        seq=seq,
                        scale=scale,
                        rotation_degrees=angle,
                    )
                    if self._sequence_lands_in_rect(
                        transformed,
                        current_x,
                        current_y,
                        rect_left,
                        rect_top,
                        rect_right,
                        rect_bottom,
                    ):
                        matching.append(transformed)
                        if len(matching) >= 10:
                            logger.info(
                                "Found at least %s transformed sequences matching rect with up to %.1f%% scaling and %s degrees rotation after %s full transforms (%s endpoint candidates)",
                                len(matching),
                                scale_pct * 100,
                                max_rot_deg,
                                transformed_attempts,
                                analytic_candidates,
                            )
                            return random.choice(matching)

            logger.info(
                "Found %s transformed sequences matching rect with up to %.1f%% scaling and %s degrees rotation after %s full transforms (%s endpoint candidates)",
                len(matching),
                scale_pct * 100,
                max_rot_deg,
                transformed_attempts,
                analytic_candidates,
            )

            if matching:
                best_available = matching

        if best_available:
            logger.info(
                "Could not find 10 transformed matches; returning a random choice from the widest tier with %s matches",
                len(best_available),
            )
            return random.choice(best_available)

        logger.info("No transformed sequences matched the rect")
        return None
