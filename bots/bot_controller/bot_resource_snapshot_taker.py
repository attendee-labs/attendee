import datetime
import logging
from collections import defaultdict

from django.utils import timezone

from bots.models import Bot, BotResourceSnapshot

logger = logging.getLogger(__name__)


from pathlib import Path


def get_process_memory_list():
    """
    Scan /proc and return a list of process *names* with their proportional
    set size (PSS) memory usage aggregated across all PIDs with that name.

    Returns a list of dicts:
        [
            {"memory_megabytes": <int MiB>, "name": <str>, "memory_percentage": <float>},
            ...
        ]

    Sorted by memory descending (largest first).
    """
    proc_root = Path("/proc")
    memory_by_name_kb = defaultdict(int)

    for entry in proc_root.iterdir():
        # Only numeric dirs are PIDs
        if not entry.name.isdigit():
            continue

        pid_dir = entry
        smaps_rollup_path = pid_dir / "smaps_rollup"
        comm_path = pid_dir / "comm"

        try:
            # Read PSS from smaps_rollup (kB)
            pss_kb = None
            with smaps_rollup_path.open() as f:
                for line in f:
                    # Example: "Pss:          12345 kB"
                    if line.startswith("Pss:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            pss_kb = int(parts[1])
                        break

            if pss_kb is None:
                continue

            # Get a human-ish name; fall back to something generic if missing
            try:
                name = comm_path.read_text().strip() or "unknown"
            except FileNotFoundError:
                name = "unknown"

            # Aggregate by name
            memory_by_name_kb[name] += pss_kb

        except (FileNotFoundError, ProcessLookupError, PermissionError):
            # Process may have exited or we might not have perms; just skip
            continue

    # Convert to list of dicts in MiB
    processes = [
        {
            "name": name,
            "memory_megabytes": int(total_kb / 1024),  # kB → MiB (approx)
        }
        for name, total_kb in memory_by_name_kb.items()
    ]

    # Sort by memory descending (largest first)
    processes.sort(key=lambda p: p["memory_megabytes"], reverse=True)

    # Get total memory and add a percentage of the total memory to the processes
    total_memory = sum(p["memory_megabytes"] for p in processes) or 1
    for process in processes:
        process["memory_percentage"] = process["memory_megabytes"] / total_memory * 100

    # Take top 5 “names”
    top_5_processes = processes[:5]
    return top_5_processes


def _detect_cgroup_layout():
    """Return paths to the usage and stat files for this container."""
    # cgroup v2 has /sys/fs/cgroup/cgroup.controllers
    if Path("/sys/fs/cgroup/cgroup.controllers").exists():
        root = Path("/sys/fs/cgroup")  # unified v2 mount
        usage_file = root / "memory.current"  # bytes
        stat_file = root / "memory.stat"
    else:  # cgroup v1
        root = Path("/sys/fs/cgroup")
        usage_file = root / "memory" / "memory.usage_in_bytes"
        stat_file = root / "memory" / "memory.stat"
    return usage_file, stat_file


def _read_first_match(path: Path, key: str, default: int = 0) -> int:
    """Parse `/sys/fs/cgroup/*/memory.stat` and return the integer after *key*."""
    try:
        with path.open() as fh:
            for line in fh:
                if line.startswith(key):
                    return int(line.split()[1])
    except FileNotFoundError:
        pass
    return default


def container_memory_mib() -> int:
    usage_path, stat_path = _detect_cgroup_layout()

    # Raw usage: everything the pod is holding.
    with usage_path.open() as fh:
        usage_bytes = int(fh.read().strip())

    # Reclaimable cache: what metrics-server subtracts.
    inactive_file = _read_first_match(stat_path, "inactive_file")

    working_set = max(usage_bytes - inactive_file, 0)
    return working_set // (1024 * 1024)


def _detect_cpu_files():
    """
    Return (usage_path, scale) where:
      * usage_path is a Path that yields a growing CPU-usage counter
      * scale converts that counter’s units into millicores/second
        (10**6 for cgroup v1 nanoseconds, 10**3 for v2 microseconds)
    """
    # unified cgroup v2 mount has this file
    if Path("/sys/fs/cgroup/cgroup.controllers").exists():
        return Path("/sys/fs/cgroup/cpu.stat"), 1_000  # µs
    # legacy cgroup v1 layout
    return Path("/sys/fs/cgroup/cpuacct/cpuacct.usage"), 1_000_000  # ns


def _read_cpu_usage(path: Path, scale: int) -> int:
    """
    Read the cumulative CPU usage, already divided by *scale* so that
    1 unit = 1 millicore×second.
    """
    if "cpu.stat" in str(path):
        # cgroup v2 – grab `usage_usec` (first field of cpu.stat)
        with path.open() as fh:
            for line in fh:
                if line.startswith("usage_usec"):
                    return int(line.split()[1]) // scale  # µs → mcore·s
        raise RuntimeError("usage_usec not found in cpu.stat")
    # cgroup v1 – cpuacct.usage (ns)
    return int(path.read_text().strip()) // scale  # ns → mcore·s


def get_cpu_usage_millicores():
    usage_file, scale = _detect_cpu_files()
    return _read_cpu_usage(usage_file, scale)


def pod_cpu_millicores(window_seconds: int, u0: int, u1: int) -> int:
    """
    Sample the container’s CPU counter twice `window` seconds apart and
    return the average use in **millicores**.
    """

    delta_mcore_seconds = max(u1 - u0, 0)
    return int(delta_mcore_seconds / window_seconds)  # average over the window


class BotResourceSnapshotTaker:
    """
    A class to handle taking snapshots of bot resource usage (CPU, RAM).
    """

    def __init__(self, bot: Bot):
        """
        Initializes the snapshot taker for a specific bot.

        It fetches the last snapshot time from the database once upon creation to
        minimize database queries.
        """
        self.bot = bot
        self._last_snapshot_time = timezone.now()
        self._first_cpu_usage_millicores = None
        self._first_cpu_usage_sample_time = None

    def save_snapshot_if_needed(self):
        if not self.bot.save_resource_snapshots():
            return

        now = timezone.now()

        # If it is more than 30 seconds since the last snapshot, sample the cpu usage.
        if self._first_cpu_usage_millicores is None and (now - self._last_snapshot_time) > datetime.timedelta(seconds=30):
            try:
                self._first_cpu_usage_millicores = get_cpu_usage_millicores()
                self._first_cpu_usage_sample_time = now
            except Exception as e:
                logger.error(f"Error getting first cpu usage for bot {self.bot.object_id}: {e}")
                return

        # Don't take a snapshot if it's been less than 1 minutes since the last snapshot.
        if (now - self._last_snapshot_time) < datetime.timedelta(minutes=1):
            return

        # Update the last snapshot time in memory for subsequent checks
        self._last_snapshot_time = now
        ram_usage_megabytes = None
        cpu_usage_millicores_delta_per_second = None

        try:
            ram_usage_megabytes = container_memory_mib()
        except Exception as e:
            # Could log this error, but for now we will just skip taking the snapshot.
            logger.error(f"Error getting memory usage for bot {self.bot.object_id}: {e}")
            return

        if self._first_cpu_usage_millicores is not None:
            try:
                second_cpu_usage_millicores = get_cpu_usage_millicores()
                cpu_usage_millicores_delta_seconds = (now - self._first_cpu_usage_sample_time).total_seconds()
                cpu_usage_millicores_delta_per_second = pod_cpu_millicores(cpu_usage_millicores_delta_seconds, self._first_cpu_usage_millicores, second_cpu_usage_millicores)
                self._first_cpu_usage_millicores = None
                self._first_cpu_usage_sample_time = None
            except Exception as e:
                logger.error(f"Error getting second cpu usage for bot {self.bot.object_id}: {e}")
                return

        if ram_usage_megabytes is None or cpu_usage_millicores_delta_per_second is None:
            logger.error(f"Error getting resource usage for bot {self.bot.object_id}: {ram_usage_megabytes} or {cpu_usage_millicores_delta_per_second} was None")
            return

        processes = []
        try:
            processes = get_process_memory_list()
        except Exception as e:
            logger.error(f"Error getting process memory list for bot {self.bot.object_id}: {e}. Continuing...")

        snapshot_data = {
            "ram_usage_megabytes": ram_usage_megabytes,
            "cpu_usage_millicores": cpu_usage_millicores_delta_per_second,
            "processes": processes,
        }

        BotResourceSnapshot.objects.create(bot=self.bot, data=snapshot_data)

        logger.info(f"Saved resource snapshot for bot {self.bot.object_id}: {snapshot_data}")
