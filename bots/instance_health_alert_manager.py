import logging
import os
from enum import Enum

import requests
from django.conf import settings

from .instance_health_utils import (
    CONNECTION_DANGER_PERCENTAGE,
    WORKER_SILENCE_CONFIRMED_AFTER_SECONDS,
    _latest_reading,
    celery_workers_have_been_down_for,
)
from .models import InstanceHealthAlertsState

logger = logging.getLogger(__name__)


class InstanceHealthAlertTypes(str, Enum):
    CONNECTIONS_USED_PERCENTAGE_EXCEEDS_THRESHOLD = "connections_used_percentage_exceeds_threshold"
    DATABASE_SIZE_EXCEEDS_THRESHOLD = "database_size_exceeds_threshold"
    CELERY_WORKERS_DOWN = "celery_workers_down"


DEFAULT_ALERT_STATE = {"active": False}

BYTES_PER_GIGABYTE = 1024 * 1024 * 1024

DEFAULT_ALERT_SETTINGS = {
    InstanceHealthAlertTypes.CONNECTIONS_USED_PERCENTAGE_EXCEEDS_THRESHOLD: {
        "enabled": True,
        "threshold": CONNECTION_DANGER_PERCENTAGE,
    },
    InstanceHealthAlertTypes.DATABASE_SIZE_EXCEEDS_THRESHOLD: {
        "enabled": True,
        "threshold": 100 * BYTES_PER_GIGABYTE,
    },
    InstanceHealthAlertTypes.CELERY_WORKERS_DOWN: {
        "enabled": True,
        "threshold": WORKER_SILENCE_CONFIRMED_AFTER_SECONDS,
    },
}

# Presentation metadata for each alert, kept alongside the defaults so the settings UI
# and its form parsing share one source of truth. Thresholds are stored raw (a
# percentage, a byte count) but shown in the unit an operator would actually type, so
# each alert carries a factor for converting between the stored and displayed value.
ALERT_METADATA = {
    InstanceHealthAlertTypes.CONNECTIONS_USED_PERCENTAGE_EXCEEDS_THRESHOLD: {
        "label": "Database connections in use",
        "description": "Fires when the share of the database connection pool in use reaches the threshold.",
        "unit_label": "%",
        "step": "1",
        "display_factor": 1,
    },
    InstanceHealthAlertTypes.DATABASE_SIZE_EXCEEDS_THRESHOLD: {
        "label": "Database size",
        "description": "Fires when the total size of the database reaches the threshold.",
        "unit_label": "GB",
        "step": "0.1",
        "display_factor": BYTES_PER_GIGABYTE,
    },
    InstanceHealthAlertTypes.CELERY_WORKERS_DOWN: {
        "label": "Celery workers down",
        "description": "Fires when no Celery worker has a live execution process for this long.",
        "unit_label": "minutes",
        "step": "1",
        "display_factor": 60,
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

    if alert is InstanceHealthAlertTypes.CELERY_WORKERS_DOWN:
        return celery_workers_have_been_down_for(config["threshold"])

    return False


def _alert_settings_with_defaults(alert_settings, alert):
    """Configuration for one alert, with any missing fields filled from its defaults."""
    stored = alert_settings.get(alert.value) or {}
    return {**DEFAULT_ALERT_SETTINGS[alert], **stored}


def _alert_state_with_defaults(alert_state, alert):
    """Firing state for one alert, with any missing fields filled from the default."""
    stored = alert_state.get(alert.value) or {}
    return {**DEFAULT_ALERT_STATE, **stored}


def _threshold_to_display(alert, raw_threshold):
    """Convert a stored threshold into the unit shown in the settings form."""
    factor = ALERT_METADATA[alert]["display_factor"]
    value = raw_threshold / factor
    # Keep whole numbers whole so the form does not show a trailing ".0".
    return int(value) if float(value).is_integer() else round(value, 2)


def _threshold_from_display(alert, display_value):
    """Convert a value typed into the settings form back into a stored threshold."""
    factor = ALERT_METADATA[alert]["display_factor"]
    raw = display_value * factor
    return int(raw) if float(raw).is_integer() else raw


def get_alert_configs(alerts_state):
    """Return one row per alert describing its configuration and current firing state.

    This is what the settings UI renders and pre-fills its form from, so it merges the
    stored configuration with the defaults and exposes thresholds in display units.
    """
    configs = []
    for alert in InstanceHealthAlertTypes:
        settings_for_alert = _alert_settings_with_defaults(alerts_state.settings, alert)
        state_for_alert = _alert_state_with_defaults(alerts_state.state, alert)
        metadata = ALERT_METADATA[alert]
        configs.append(
            {
                "key": alert.value,
                "label": metadata["label"],
                "description": metadata["description"],
                "unit_label": metadata["unit_label"],
                "step": metadata["step"],
                "enabled": bool(settings_for_alert.get("enabled")),
                "threshold": _threshold_to_display(alert, settings_for_alert["threshold"]),
                "active": bool(state_for_alert.get("active")),
            }
        )
    return configs


def update_alert_settings(alerts_state, form_data):
    """Persist enabled flags and thresholds submitted by the settings form.

    Every alert is rewritten from the form so an unchecked checkbox (absent from the
    POST) is correctly read as disabled. A blank or unparseable threshold leaves the
    existing one untouched rather than wiping it.
    """
    new_settings = {}
    for alert in InstanceHealthAlertTypes:
        current = _alert_settings_with_defaults(alerts_state.settings, alert)
        threshold = current["threshold"]

        raw_value = form_data.get(f"{alert.value}__threshold")
        if raw_value not in (None, ""):
            try:
                threshold = _threshold_from_display(alert, float(raw_value))
            except (TypeError, ValueError):
                pass

        new_settings[alert.value] = {
            **current,
            "enabled": form_data.get(f"{alert.value}__enabled") is not None,
            "threshold": threshold,
        }

    alerts_state.settings = new_settings
    alerts_state.save(update_fields=["settings", "updated_at"])


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
                newly_cleared = sorted(alert for alert in InstanceHealthAlertTypes if alert not in active and (alerts_state.state.get(alert.value) or {}).get("active"))
                logger.info(
                    "Instance health alert state changed (newly active: %s, cleared: %s)",
                    [alert.value for alert in newly_active] or "none",
                    [alert.value for alert in newly_cleared] or "none",
                )
                alerts_state.state = new_state
                alerts_state.save(update_fields=["state", "updated_at"])
                _notify_slack_of_alert_changes(newly_active, newly_cleared)
        except Exception:
            logger.exception("Failed to update instance health alerts")


def _notify_slack_of_alert_changes(newly_active, newly_cleared):
    """Post a single Slack message summarizing which alerts started and stopped firing.

    Only sends if SLACK_WEBHOOK_URL_FOR_INSTANCE_HEALTH_ALERTS is configured. Any
    failure is swallowed so alerting never disrupts the scheduler loop.
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL_FOR_INSTANCE_HEALTH_ALERTS")
    if not webhook_url:
        return

    if not newly_active and not newly_cleared:
        return

    lines = ["*Attendee instance health alerts update*"]
    if newly_active:
        lines.append("")
        lines.append(":rotating_light: Now firing:")
        lines.extend(f"• {ALERT_METADATA[alert]['label']}" for alert in newly_active)
    if newly_cleared:
        lines.append("")
        lines.append(":white_check_mark: No longer firing:")
        lines.extend(f"• {ALERT_METADATA[alert]['label']}" for alert in newly_cleared)

    lines.append("")
    lines.append("Head to the instance health dashboard to see the metrics.")

    try:
        response = requests.post(webhook_url, json={"text": "\n".join(lines)}, timeout=4)
        response.raise_for_status()
    except Exception:
        logger.warning("Failed to send instance health alert Slack notification", exc_info=True)
