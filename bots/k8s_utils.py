import json
import logging

from kubernetes import client

logger = logging.getLogger(__name__)


def find_bot_pod(v1, namespace, bot):
    """Return the pod running this bot, or None if there isn't one.

    The bot may be running on a pod created specifically for it or on a bot runner pod
    that was assigned to it, so we look up the pod by the bot's pod name first and fall
    back to looking up by the assigned-bot-id label. Listing pods requires an extra RBAC
    permission that some deployments don't grant, so if we can't list we treat the pod
    as not found. Assumes there is at most one pod per bot.
    """
    try:
        return v1.read_namespaced_pod(name=bot.k8s_pod_name(), namespace=namespace)
    except client.ApiException as e:
        if e.status != 404:
            raise

    try:
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"assigned-bot-id={bot.object_id}")
    except client.ApiException as e:
        if e.status == 403:
            logger.warning("Not permitted to list pods in namespace %s, so cannot look up pod for bot %s by label: %s", namespace, bot.object_id, e)
            return None
        raise

    return pods.items[0] if pods.items else None


def find_bot_pod_name(v1, namespace, bot):
    """Return the name of the pod running this bot, falling back to the bot's default pod name if no pod is found."""
    pod = find_bot_pod(v1, namespace, bot)
    return pod.metadata.name if pod else bot.k8s_pod_name()


def retrieve_bot_pod_information(v1, namespace, pod_name):
    """Best-effort snapshot of a bot pod's infrastructure state.

    Reads the pod's current status and its recent Kubernetes events. The container
    waiting/terminated reasons (e.g. ``ImagePullBackOff``, ``CreateContainerError``)
    persist on the pod object even after Kubernetes Events expire (~1h), so this is
    still useful when called well after the failure.

    Always returns a JSON-serializable dict and never raises, so callers can attach
    the result to event metadata without risking the surrounding workflow.
    """
    try:
        return _retrieve_bot_pod_information(v1, namespace, pod_name)
    except Exception as e:
        logger.warning("Failed to gather bot pod information for %s: %s", pod_name, e)
        return {"pod_name": pod_name, "diagnostics_error": str(e)}


def _retrieve_bot_pod_information(v1, namespace, pod_name):
    diagnostics = {"pod_name": pod_name}

    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except client.ApiException as e:
        diagnostics["pod_found"] = False
        diagnostics["pod_read_error"] = "not_found" if e.status == 404 else str(e)
        pod = None

    if pod is not None:
        status = getattr(pod, "status", None)
        diagnostics["pod_found"] = True
        diagnostics["phase"] = getattr(status, "phase", None)
        diagnostics["reason"] = getattr(status, "reason", None)
        diagnostics["message"] = getattr(status, "message", None)
        diagnostics["conditions"] = [
            {
                "type": getattr(c, "type", None),
                "status": getattr(c, "status", None),
                "reason": getattr(c, "reason", None),
                "message": getattr(c, "message", None),
            }
            for c in (getattr(status, "conditions", None) or [])
        ]
        diagnostics["container_statuses"] = [_summarize_container_status(cs) for cs in (getattr(status, "container_statuses", None) or [])]

    try:
        events = v1.list_namespaced_event(namespace=namespace, field_selector=f"involvedObject.name={pod_name}")
        diagnostics["events"] = [_summarize_event(ev) for ev in (getattr(events, "items", None) or [])]
    except Exception as e:
        diagnostics["events_error"] = str(e)

    # Guarantee serializability so attaching diagnostics can never block recording the failure.
    try:
        return json.loads(json.dumps(diagnostics, default=str))
    except (TypeError, ValueError):
        return {"pod_name": pod_name, "serialization_error": True}


def _summarize_container_status(cs):
    summary = {
        "name": getattr(cs, "name", None),
        "ready": getattr(cs, "ready", None),
        "restart_count": getattr(cs, "restart_count", None),
    }
    state = getattr(cs, "state", None)
    waiting = getattr(state, "waiting", None)
    terminated = getattr(state, "terminated", None)
    running = getattr(state, "running", None)
    if waiting is not None:
        summary["state"] = "waiting"
        summary["reason"] = getattr(waiting, "reason", None)
        summary["message"] = getattr(waiting, "message", None)
    elif terminated is not None:
        summary["state"] = "terminated"
        summary["reason"] = getattr(terminated, "reason", None)
        summary["message"] = getattr(terminated, "message", None)
        summary["exit_code"] = getattr(terminated, "exit_code", None)
    elif running is not None:
        summary["state"] = "running"
    return summary


def _summarize_event(ev):
    last_timestamp = getattr(ev, "last_timestamp", None)
    return {
        "type": getattr(ev, "type", None),
        "reason": getattr(ev, "reason", None),
        "message": getattr(ev, "message", None),
        "count": getattr(ev, "count", None),
        "last_timestamp": last_timestamp.isoformat() if hasattr(last_timestamp, "isoformat") else last_timestamp,
    }
