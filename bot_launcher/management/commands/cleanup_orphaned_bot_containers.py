"""
Cleanup command to remove orphaned ephemeral bot containers.

Usage:
    python manage.py cleanup_orphaned_bot_containers
    python manage.py cleanup_orphaned_bot_containers --dry-run
    python manage.py cleanup_orphaned_bot_containers --max-age-hours 24
"""
import logging
from datetime import datetime, timedelta

import docker
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Cleans up orphaned or too old ephemeral bot containers"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Shows containers that would be removed without removing them",
        )
        parser.add_argument(
            "--max-age-hours",
            type=int,
            default=5,  # 5h default (beyond 4h max execution)
            help="Remove containers older than X hours (default: 5)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        max_age_hours = options["max_age_hours"]
        max_age = timedelta(hours=max_age_hours)

        try:
            client = docker.from_env()
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Cannot connect to Docker: {e}"))
            return

        # Get all containers with label attendee.type=ephemeral-bot
        try:
            containers = client.containers.list(
                all=True, filters={"label": "attendee.type=ephemeral-bot"}
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Error retrieving containers: {e}"))
            return

        if not containers:
            self.stdout.write(self.style.SUCCESS("✅ No ephemeral containers found"))
            return

        self.stdout.write(f"📋 {len(containers)} ephemeral container(s) found")

        now = datetime.now()
        to_remove = []

        for container in containers:
            # Check container age
            created_at = datetime.fromtimestamp(container.attrs["Created"])
            age = now - created_at

            # Check status
            status = container.status
            bot_id = container.labels.get("attendee.bot_id", "unknown")

            should_remove = False
            reason = ""

            if status == "exited":
                should_remove = True
                reason = f"Container stopped (status: {status})"
            elif age > max_age:
                should_remove = True
                reason = f"Container too old ({age.total_seconds() / 3600:.1f}h > {max_age_hours}h)"
            elif status not in ["running", "restarting"]:
                should_remove = True
                reason = f"Container in abnormal state (status: {status})"

            if should_remove:
                to_remove.append((container, reason, age, bot_id))

        if not to_remove:
            self.stdout.write(self.style.SUCCESS("✅ No containers to clean up"))
            return

        self.stdout.write(f"\n🗑️  {len(to_remove)} container(s) to remove:")

        for container, reason, age, bot_id in to_remove:
            age_str = f"{age.total_seconds() / 3600:.1f}h"
            self.stdout.write(
                f"  - bot-{bot_id} ({container.short_id[:12]}) - {reason} - age: {age_str}"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("\n⚠️  Dry-run mode: no containers were removed"))
            return

        # Remove containers
        removed_count = 0
        for container, reason, age, bot_id in to_remove:
            try:
                if container.status == "running":
                    self.stdout.write(f"  🛑 Stopping bot-{bot_id}...")
                    container.stop(timeout=10)
                container.remove()
                removed_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  ✅ bot-{bot_id} removed")
                )
            except docker.errors.NotFound:
                # Container already removed
                self.stdout.write(f"  ⚠️  bot-{bot_id} already removed")
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"  ❌ Error removing bot-{bot_id}: {e}")
                )

        self.stdout.write(
            self.style.SUCCESS(f"\n✅ Cleanup completed: {removed_count}/{len(to_remove)} container(s) removed")
        )
