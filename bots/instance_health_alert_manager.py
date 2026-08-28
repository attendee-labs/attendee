import logging
from enum import Enum

from django.conf import settings

from .instance_health_utils import CONNECTION_DANGER_PERCENTAGE, _latest_reading
from .models import InstanceHealthAlertsState

logger = logging.getLogger(__name__)


class InstanceHealthAlertTypes(str, Enum):
    CONNECTIONS_USED_PERCENTAGE_EXCEEDS_THRESHOLD = "connections_used_percentage_exceeds_threshold"
    DATABASE_SIZE_EXCEEDS_THRESHOLD = "database_size_exceeds_threshold"


DEFAULT_ALERT_STATE = {"activated_at": None}

DEFAULT_ALERT_SETTINGS = {
    InstanceHealthAlertTypes.CONNECTIONS_USED_PERCENTAGE_EXCEEDS_THRESHOLD: {
        "enabled": True,
        "threshold": CONNECTION_DANGER_PERCENTAGE,
    },
    InstanceHealthAlertTypes.DATABASE_SIZE_EXCEEDS_THRESHOLD: {
        "enabled": True,
        "threshold": 100 * 1024 * 1024 * 1024,
    },
}


def _alert_is_firing(alert, config):
    if alert is InstanceHealthAlertTypes.CONNECTIONS_USED_PERCENTAGE_EXCEEDS_THRESHOLD:
        connections, _ = _latest_reading("database_connections")
        used_percentage = (connections or {}).get("used_percentage")
        return used_percentage is not None and used_percentage >= config["threshold"]

    if alert is InstanceHealthAlertTypes.DATABASE_SIZE_EXCEEDS_THRESHOLD:
        table_sizes, _ = _latest_reading("database_table_sizes")
        total_bytes = (table_sizes or {}).get("total_bytes")
        return total_bytes is not None and total_bytes >= config["threshold"]

    return False


def _alert_settings_with_defaults(alert_settings, alert):
    """Configuration for one alert, with any missing fields filled from its defaults."""
    stored = alert_settings.get(alert.value) or {}
    return {**DEFAULT_ALERT_SETTINGS[alert], **stored}


def _alert_state_with_defaults(alert_state, alert):
    """Firing state for one alert, with any missing fields filled from the default."""
    stored = alert_state.get(alert.value) or {}
    return {**DEFAULT_ALERT_STATE, **stored}


def compute_active_alerts(alert_settings):
    """Return the set of alerts that are both enabled and firing right now.

    Each alert is evaluated against the latest snapshot carrying its metric, so this
    reflects current values however often that metric is sampled. A disabled alert is
    never active, whatever its metric is doing.
    """
    active = set()
    for alert in InstanceHealthAlertTypes:
        config = _alert_settings_with_defaults(alert_settings, alert)
        if not config.get("enabled"):
            continue
        if _alert_is_firing(alert, config):
            active.add(alert)
    return active


class InstanceHealthAlertManager:
    """Reconciles instance health alert firing state against the latest snapshots."""

    def update_alerts(self):
        if not settings.SAVE_INSTANCE_HEALTH_SNAPSHOTS:
            return

        # Never let an alerting failure propagate: this runs inside the scheduler loop
        # and an exception here would skip the rest of that cycle's work.
        try:
            alerts_state = InstanceHealthAlertsState.load()
            active = compute_active_alerts(alerts_state.settings)

            new_state = {alert.value: {**_alert_state_with_defaults(alerts_state.state, alert), "active": alert in active} for alert in InstanceHealthAlertTypes}

            if new_state != alerts_state.state:
                newly_active = sorted(alert for alert in active if not (alerts_state.state.get(alert.value) or {}).get("active"))
                newly_cleared = sorted(alert.value for alert in InstanceHealthAlertTypes if alert not in active and (alerts_state.state.get(alert.value) or {}).get("active"))
                logger.info(
                    "Instance health alert state changed (newly active: %s, cleared: %s)",
                    [alert.value for alert in newly_active] or "none",
                    newly_cleared or "none",
                )
                alerts_state.state = new_state
                alerts_state.save(update_fields=["state", "updated_at"])
        except Exception:
            logger.exception("Failed to update instance health alerts")
