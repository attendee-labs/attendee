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


@dataclass
class _TransformCandidate:
    continuous_distance_to_rect: float
    scale_deviation: float
    rotation_deviation: float
    tier_index: int
    seq: PrimitiveMocapSequence
    scale: float
    rotation_degrees: float


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
    def _clamp(value: float, lower: float, upper: float) -> float:
        return min(max(value, lower), upper)

    @staticmethod
    def _point_distance_to_rect(
        x: float,
        y: float,
        rect_left: float,
        rect_top: float,
        rect_right: float,
        rect_bottom: float,
    ) -> float:
        dx = max(rect_left - x, 0.0, x - rect_right)
        dy = max(rect_top - y, 0.0, y - rect_bottom)
        return math.hypot(dx, dy)

    def _sequence_distance_to_rect(
        self,
        seq: PrimitiveMocapSequence,
        current_x: int,
        current_y: int,
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> float:
        final_x = current_x + seq.total_dx
        final_y = current_y + seq.total_dy
        return self._point_distance_to_rect(final_x, final_y, rect_left, rect_top, rect_right, rect_bottom)

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

    def _probe_points_for_rect(
        self,
        rect_left: float,
        rect_top: float,
        rect_right: float,
        rect_bottom: float,
        preferred_x: float,
        preferred_y: float,
    ) -> list[tuple[float, float]]:
        center_x = (rect_left + rect_right) / 2.0
        center_y = (rect_top + rect_bottom) / 2.0
        closest_to_preferred_x = self._clamp(preferred_x, rect_left, rect_right)
        closest_to_preferred_y = self._clamp(preferred_y, rect_top, rect_bottom)

        def halfway_from_center_to(x: float, y: float) -> tuple[float, float]:
            return (
                (center_x + x) / 2.0,
                (center_y + y) / 2.0,
            )

        return [
            (center_x, center_y),
            (closest_to_preferred_x, closest_to_preferred_y),
            # Interior points halfway between the center and each corner.
            halfway_from_center_to(rect_left, rect_top),
            halfway_from_center_to(rect_left, rect_bottom),
            halfway_from_center_to(rect_right, rect_top),
            halfway_from_center_to(rect_right, rect_bottom),
            # Edge midpoints are still useful and not as extreme as corners.
            (center_x, rect_top),
            (center_x, rect_bottom),
            (rect_left, center_y),
            (rect_right, center_y),
        ]

    def _candidate_rotations_for_rect(
        self,
        seq: PrimitiveMocapSequence,
        rect_left: float,
        rect_top: float,
        rect_right: float,
        rect_bottom: float,
        max_rot_deg: float,
    ) -> list[float]:
        base_angle = math.degrees(math.atan2(seq.total_dy, seq.total_dx))
        probes = self._probe_points_for_rect(
            rect_left=rect_left,
            rect_top=rect_top,
            rect_right=rect_right,
            rect_bottom=rect_bottom,
            preferred_x=seq.total_dx,
            preferred_y=seq.total_dy,
        )

        rotations = {-max_rot_deg, 0.0, max_rot_deg}
        for x, y in probes:
            if abs(x) < 1e-9 and abs(y) < 1e-9:
                continue
            target_angle = math.degrees(math.atan2(y, x))
            rotation = self._normalize_angle_degrees(target_angle - base_angle)
            rotations.add(self._clamp(rotation, -max_rot_deg, max_rot_deg))

        rotations_list = list(rotations)
        random.shuffle(rotations_list)
        return rotations_list

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
            if rect_left <= 0.0 <= rect_right and rect_top <= 0.0 <= rect_bottom:
                return [(1.0, 0.0)]
            return []

        base_angle = math.degrees(math.atan2(seq.total_dy, seq.total_dx))
        min_radius = seq_len * (1.0 - scale_pct)
        max_radius = seq_len * (1.0 + scale_pct)

        params: list[tuple[float, float]] = []
        seen: set[tuple[float, float]] = set()

        for rotation in self._candidate_rotations_for_rect(
            seq=seq,
            rect_left=rect_left,
            rect_top=rect_top,
            rect_right=rect_right,
            rect_bottom=rect_bottom,
            max_rot_deg=max_rot_deg,
        ):
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

            # Try one radius that minimizes distortion, and one radius centered in
            # the overlap to reduce the chance that per-movement rounding pushes
            # the final endpoint just outside the rect.
            radii = [
                self._clamp(seq_len, overlap_min, overlap_max),
                (overlap_min + overlap_max) / 2.0,
            ]
            for radius in radii:
                scale = radius / seq_len
                key = (round(scale, 6), round(rotation, 6))
                if key in seen:
                    continue
                seen.add(key)
                params.append((scale, rotation))

        return params

    def _best_effort_transform_candidate_for_rect(
        self,
        seq: PrimitiveMocapSequence,
        rect_left: float,
        rect_top: float,
        rect_right: float,
        rect_bottom: float,
        scale_pct: float,
        max_rot_deg: float,
        tier_index: int,
    ) -> _TransformCandidate:
        """
        Return the lowest-distance endpoint transform found by endpoint-only math.

        This is used when no transformed sequence actually lands in the rect. It
        intentionally does not materialize transformed movements; the caller only
        does that for the best few endpoint candidates.
        """
        seq_len = math.hypot(seq.total_dx, seq.total_dy)
        if seq_len < 1e-9:
            return _TransformCandidate(
                continuous_distance_to_rect=self._point_distance_to_rect(
                    0.0,
                    0.0,
                    rect_left,
                    rect_top,
                    rect_right,
                    rect_bottom,
                ),
                scale_deviation=0.0,
                rotation_deviation=0.0,
                tier_index=tier_index,
                seq=seq,
                scale=1.0,
                rotation_degrees=0.0,
            )

        base_angle = math.degrees(math.atan2(seq.total_dy, seq.total_dx))
        min_radius = seq_len * (1.0 - scale_pct)
        max_radius = seq_len * (1.0 + scale_pct)

        best: _TransformCandidate | None = None

        for rotation in self._candidate_rotations_for_rect(
            seq=seq,
            rect_left=rect_left,
            rect_top=rect_top,
            rect_right=rect_right,
            rect_bottom=rect_bottom,
            max_rot_deg=max_rot_deg,
        ):
            endpoint_angle = base_angle + rotation
            ray_interval = self._ray_rect_radius_interval(
                endpoint_angle,
                rect_left,
                rect_top,
                rect_right,
                rect_bottom,
            )

            if ray_interval is not None:
                ray_min, ray_max = ray_interval
                overlap_min = max(ray_min, min_radius)
                overlap_max = min(ray_max, max_radius)
                if overlap_min <= overlap_max:
                    # Exact continuous hit. Prefer scale closest to 1.0.
                    radius = self._clamp(seq_len, overlap_min, overlap_max)
                    scale = radius / seq_len
                    candidate = _TransformCandidate(
                        continuous_distance_to_rect=0.0,
                        scale_deviation=abs(scale - 1.0),
                        rotation_deviation=abs(rotation),
                        tier_index=tier_index,
                        seq=seq,
                        scale=scale,
                        rotation_degrees=rotation,
                    )
                    if best is None or self._candidate_sort_key(candidate) < self._candidate_sort_key(best):
                        best = candidate
                    continue

            # No exact continuous intersection for this ray. Minimize distance
            # from the allowed segment on the ray to the rect with a cheap ternary
            # search. Distance to a convex rect along a line is convex.
            rad = math.radians(endpoint_angle)
            cos_a = math.cos(rad)
            sin_a = math.sin(rad)

            lo = min_radius
            hi = max_radius
            for _ in range(24):
                m1 = lo + (hi - lo) / 3.0
                m2 = hi - (hi - lo) / 3.0
                d1 = self._point_distance_to_rect(
                    m1 * cos_a,
                    m1 * sin_a,
                    rect_left,
                    rect_top,
                    rect_right,
                    rect_bottom,
                )
                d2 = self._point_distance_to_rect(
                    m2 * cos_a,
                    m2 * sin_a,
                    rect_left,
                    rect_top,
                    rect_right,
                    rect_bottom,
                )
                if d1 < d2:
                    hi = m2
                else:
                    lo = m1

            radius = (lo + hi) / 2.0
            scale = radius / seq_len
            distance = self._point_distance_to_rect(
                radius * cos_a,
                radius * sin_a,
                rect_left,
                rect_top,
                rect_right,
                rect_bottom,
            )
            candidate = _TransformCandidate(
                continuous_distance_to_rect=distance,
                scale_deviation=abs(scale - 1.0),
                rotation_deviation=abs(rotation),
                tier_index=tier_index,
                seq=seq,
                scale=scale,
                rotation_degrees=rotation,
            )
            if best is None or self._candidate_sort_key(candidate) < self._candidate_sort_key(best):
                best = candidate

        # _candidate_rotations_for_rect always returns at least {-max, 0, max}, so
        # best should always be set for a non-zero endpoint. This guard is just to
        # keep the return type total.
        if best is not None:
            return best

        return _TransformCandidate(
            continuous_distance_to_rect=self._point_distance_to_rect(
                seq.total_dx,
                seq.total_dy,
                rect_left,
                rect_top,
                rect_right,
                rect_bottom,
            ),
            scale_deviation=0.0,
            rotation_deviation=0.0,
            tier_index=tier_index,
            seq=seq,
            scale=1.0,
            rotation_degrees=0.0,
        )

    @staticmethod
    def _candidate_sort_key(candidate: _TransformCandidate) -> tuple[float, float, float, int]:
        return (
            candidate.continuous_distance_to_rect,
            candidate.scale_deviation,
            candidate.rotation_deviation,
            candidate.tier_index,
        )

    def _choose_best_materialized_fallback(
        self,
        candidates: list[_TransformCandidate],
        current_x: int,
        current_y: int,
        rect_left: int,
        rect_top: int,
        rect_right: int,
        rect_bottom: int,
    ) -> PrimitiveMocapSequence | None:
        if not candidates:
            return None

        # Materialize only the strongest endpoint candidates. This keeps the
        # fallback cheap while still accounting for per-movement rounding before
        # returning the final sequence.
        candidates = sorted(candidates, key=self._candidate_sort_key)[:25]

        best_seq: PrimitiveMocapSequence | None = None
        best_score: tuple[float, float, float, int] | None = None

        for candidate in candidates:
            transformed = self._transform_sequence(
                seq=candidate.seq,
                scale=candidate.scale,
                rotation_degrees=candidate.rotation_degrees,
            )
            actual_distance = self._sequence_distance_to_rect(
                transformed,
                current_x,
                current_y,
                rect_left,
                rect_top,
                rect_right,
                rect_bottom,
            )
            score = (
                actual_distance,
                candidate.scale_deviation,
                candidate.rotation_deviation,
                candidate.tier_index,
            )
            if best_score is None or score < best_score:
                best_score = score
                best_seq = transformed

        if best_score is not None:
            logger.info(
                "No transformed sequence landed inside rect; returning closest fallback with distance %.2fpx, scale deviation %.2f%%, rotation %.2f degrees, tier %s",
                best_score[0],
                best_score[1] * 100,
                best_score[2],
                best_score[3],
            )

        return best_seq

    # Fallback for when we absolutely need to find a motion sequence that lands in
    # the rect, even if it means stretching or rotating it. If no allowed
    # transform can actually land inside the rect after per-movement rounding, this
    # returns the closest plausible transformed sequence instead of None.
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

        if not self.sequences:
            logger.info("No mocap sequences available")
            return None

        # Normalize rect ordering defensively. Existing callers likely already pass
        # ordered bounds, but this keeps the geometry helpers sane.
        rect_left, rect_right = sorted((rect_left, rect_right))
        rect_top, rect_bottom = sorted((rect_top, rect_bottom))

        # Work in a coordinate system where the current mouse position is the
        # origin. The target rect then describes the allowed final delta vector.
        target_left = rect_left - current_x
        target_top = rect_top - current_y
        target_right = rect_right - current_x
        target_bottom = rect_bottom - current_y

        best_available_exact_matches: list[PrimitiveMocapSequence] = []
        best_effort_candidates: list[_TransformCandidate] = []
        seen_best_effort_candidates: set[tuple[int, float, float]] = set()

        for tier_index, (scale_pct, max_rot_deg) in enumerate(tiers):
            matching: list[PrimitiveMocapSequence] = []
            transformed_attempts = 0
            analytic_candidates = 0

            sequences = list(self.sequences)
            random.shuffle(sequences)

            for seq in sequences:
                best_effort = self._best_effort_transform_candidate_for_rect(
                    seq=seq,
                    rect_left=target_left,
                    rect_top=target_top,
                    rect_right=target_right,
                    rect_bottom=target_bottom,
                    scale_pct=scale_pct,
                    max_rot_deg=max_rot_deg,
                    tier_index=tier_index,
                )
                best_effort_key = (
                    id(seq),
                    round(best_effort.scale, 6),
                    round(best_effort.rotation_degrees, 6),
                )
                if best_effort_key not in seen_best_effort_candidates:
                    seen_best_effort_candidates.add(best_effort_key)
                    best_effort_candidates.append(best_effort)

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
                    else:
                        # It was a continuous endpoint candidate, but summed
                        # per-movement rounding may have pushed it out of bounds.
                        # Keep it as a fallback candidate using its actual distance.
                        distance = self._sequence_distance_to_rect(
                            transformed,
                            current_x,
                            current_y,
                            rect_left,
                            rect_top,
                            rect_right,
                            rect_bottom,
                        )
                        fallback_candidate = _TransformCandidate(
                            continuous_distance_to_rect=distance,
                            scale_deviation=abs(scale - 1.0),
                            rotation_deviation=abs(angle),
                            tier_index=tier_index,
                            seq=seq,
                            scale=scale,
                            rotation_degrees=angle,
                        )
                        fallback_key = (id(seq), round(scale, 6), round(angle, 6))
                        if fallback_key not in seen_best_effort_candidates:
                            seen_best_effort_candidates.add(fallback_key)
                            best_effort_candidates.append(fallback_candidate)

            logger.info(
                "Found %s transformed sequences matching rect with up to %.1f%% scaling and %s degrees rotation after %s full transforms (%s endpoint candidates)",
                len(matching),
                scale_pct * 100,
                max_rot_deg,
                transformed_attempts,
                analytic_candidates,
            )

            if matching:
                best_available_exact_matches = matching

        if best_available_exact_matches:
            logger.info(
                "Could not find 10 transformed matches; returning a random choice from the widest tier with %s exact matches",
                len(best_available_exact_matches),
            )
            return random.choice(best_available_exact_matches)

        return self._choose_best_materialized_fallback(
            candidates=best_effort_candidates,
            current_x=current_x,
            current_y=current_y,
            rect_left=rect_left,
            rect_top=rect_top,
            rect_right=rect_right,
            rect_bottom=rect_bottom,
        )
