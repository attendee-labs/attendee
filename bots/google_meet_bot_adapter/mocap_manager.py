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
    def __init__(self):
        self.sequences: list[PrimitiveMocapSequence] = []
        self._load_all_scramble_files()
        self._generate_perturbed_sequences()

    def _generate_perturbed_sequences(self):
        original_sequences = list(self.sequences)
        for seq in original_sequences:
            for i in range(21):
                angle = -10 + (10 / 11) * (i + 1)
                if i == 10:
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
        pattern = os.path.join(directory, "join_mocap_scramble_*_720p.json")
        file_paths = sorted(glob.glob(pattern))

        logger.info(f"Found {len(file_paths)} mocap scramble files")

        for file_path in file_paths:
            with open(file_path, "r") as f:
                events = json.load(f)
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
