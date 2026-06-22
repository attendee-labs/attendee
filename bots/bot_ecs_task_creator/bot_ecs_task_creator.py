import logging
import os
import random
import time
from typing import Dict, List, Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# fmt: off

# RunTask can fail transiently while Fargate/EC2 capacity is being provisioned or
# when the ECS control plane throttles us. These are safe to retry.
TRANSIENT_ECS_ERROR_CODES = (
    "ThrottlingException",
    "Throttling",
    "RequestLimitExceeded",
    "ServiceUnavailable",
    "InternalServerError",
    "ClusterNotActiveException",
)

# Reasons RunTask reports in `failures[]` that are worth retrying (capacity races).
TRANSIENT_ECS_FAILURE_REASONS = (
    "Capacity is unavailable at this time",
    "RESOURCE:CPU",
    "RESOURCE:MEMORY",
)


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def is_transient_ecs_client_error(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    return code in TRANSIENT_ECS_ERROR_CODES


def is_transient_ecs_failure(failures: List[dict]) -> bool:
    """True when every RunTask failure looks like a retryable capacity race."""
    if not failures:
        return False
    return all(any(marker in (f.get("reason") or "") for marker in TRANSIENT_ECS_FAILURE_REASONS) for f in failures)


def fetch_bot_task_definition(bot_pod_spec_type: Optional[str]) -> Optional[str]:
    """Resolve the task-definition family for a bot.

    Mirrors ``fetch_bot_pod_spec`` in the Kubernetes creator: a spec type selects
    a different, pre-registered task definition (e.g. a higher-memory variant).
    ``BOT_TASK_DEFINITION_<TYPE>`` overrides per type; ``BOT_TASK_DEFINITION`` is
    the default. Returns ``None`` if no default is configured.
    """
    default = os.getenv("BOT_TASK_DEFINITION")
    if not bot_pod_spec_type:
        return default

    # Out of caution, only allow uppercase alphabetic spec types in env lookups.
    if bot_pod_spec_type.isalpha() and bot_pod_spec_type.isupper():
        override = os.getenv(f"BOT_TASK_DEFINITION_{bot_pod_spec_type}")
        if override:
            return override
    return default


class BotEcsTaskCreator:
    """Launches a bot as a standalone, run-to-completion ECS task.

    The ECS analogue of ``BotPodCreator``. A bot task runs
    ``python manage.py run_bot --botid <id>`` and exits when the meeting ends, so
    there is no service or restart policy to manage. All infrastructure values are
    read from the environment so nothing about the deployment is hard-coded here.

    Environment contract (set by the deploying infrastructure):
      BOT_ECS_CLUSTER             - cluster name or ARN (required)
      BOT_TASK_DEFINITION         - default task-def family[:revision] (required)
      BOT_TASK_DEFINITION_<TYPE>  - optional per-spec-type task-def override
      BOT_TASK_SUBNETS            - comma-separated private subnet IDs (required)
      BOT_TASK_SECURITY_GROUPS    - comma-separated security group IDs (required)
      BOT_TASK_CONTAINER_NAME     - container in the task def to override (default "bot-proc")
      BOT_TASK_LAUNCH_TYPE        - FARGATE | EC2 (default FARGATE)
      BOT_TASK_CAPACITY_PROVIDER  - capacity provider name (overrides launch type if set)
      BOT_TASK_ASSIGN_PUBLIC_IP   - ENABLED | DISABLED (default DISABLED; egress via NAT)
      BOT_TASK_ENABLE_EXECUTE_COMMAND - "true" to allow ECS Exec into bot tasks (default false)
      AWS_REGION / AWS_DEFAULT_REGION - boto3 region
    """

    def __init__(self):
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        # Adaptive retries cover generic throttling; we layer our own retry on top
        # for capacity races that surface in RunTask's `failures[]` (not exceptions).
        boto_config = BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"})
        self.ecs = boto3.client("ecs", region_name=region, config=boto_config)

        self.cluster = os.getenv("BOT_ECS_CLUSTER")
        self.subnets = _split_csv(os.getenv("BOT_TASK_SUBNETS"))
        self.security_groups = _split_csv(os.getenv("BOT_TASK_SECURITY_GROUPS"))
        self.container_name = os.getenv("BOT_TASK_CONTAINER_NAME", "bot-proc")
        self.launch_type = os.getenv("BOT_TASK_LAUNCH_TYPE", "FARGATE")
        self.capacity_provider = os.getenv("BOT_TASK_CAPACITY_PROVIDER")
        self.assign_public_ip = os.getenv("BOT_TASK_ASSIGN_PUBLIC_IP", "DISABLED")
        self.enable_execute_command = os.getenv("BOT_TASK_ENABLE_EXECUTE_COMMAND", "false").lower() == "true"

    @staticmethod
    def started_by(bot_id: int) -> str:
        # ECS `startedBy` is capped at 36 chars; "bot-<id>" stays well under and lets
        # us find/stop a bot's task later via list_tasks(startedBy=...).
        return f"bot-{bot_id}"

    def _network_configuration(self) -> dict:
        return {
            "awsvpcConfiguration": {
                "subnets": self.subnets,
                "securityGroups": self.security_groups,
                "assignPublicIp": self.assign_public_ip,
            }
        }

    def _run_task_with_retry(self, run_task_kwargs: dict, description: str) -> dict:
        max_retries = int(os.getenv("BOT_TASK_CREATE_MAX_RETRIES", "3"))
        base_delay_seconds = float(os.getenv("BOT_TASK_CREATE_RETRY_DELAY_SECONDS", "2"))

        for attempt in range(max_retries + 1):
            try:
                response = self.ecs.run_task(**run_task_kwargs)
            except ClientError as exc:
                if attempt >= max_retries or not is_transient_ecs_client_error(exc):
                    raise
                self._sleep_backoff(description, attempt, max_retries, base_delay_seconds, str(exc))
                continue

            # RunTask returns a 200 even when it could not place the task; the reason
            # is in `failures[]`. Retry capacity races, surface everything else.
            failures = response.get("failures", [])
            if response.get("tasks"):
                return response
            if attempt < max_retries and is_transient_ecs_failure(failures):
                self._sleep_backoff(description, attempt, max_retries, base_delay_seconds, str(failures))
                continue
            return response

    @staticmethod
    def _sleep_backoff(description, attempt, max_retries, base_delay_seconds, detail):
        delay = base_delay_seconds * (2**attempt) + random.uniform(0, 1)
        logger.warning(
            "Transient ECS error launching %s (attempt %s/%s), retrying in %.1fs: %s",
            description, attempt + 1, max_retries + 1, delay, detail,
        )
        time.sleep(delay)

    def create_bot_task(
        self,
        bot_id: int,
        bot_pod_spec_type: Optional[str] = None,
        add_webpage_streamer: Optional[bool] = False,
        add_persistent_storage: Optional[bool] = False,
    ) -> Dict:
        """Launch a bot as an ECS task. Returns a dict with a ``created`` flag,
        shaped like ``BotPodCreator.create_bot_pod`` so callers can treat both the
        same way."""
        # These are unsupported on ECS today (voice-agent streamer sidecar and
        # PVC-backed storage are Kubernetes-only). Warn rather than fail so a bot
        # that merely requested them still launches in recording/transcription mode.
        if add_webpage_streamer:
            logger.warning("Bot %s requested a webpage streamer; not supported with LAUNCH_BOT_METHOD=ecs, ignoring", bot_id)
        if add_persistent_storage:
            logger.warning("Bot %s requested persistent storage; not supported with LAUNCH_BOT_METHOD=ecs, ignoring", bot_id)

        task_definition = fetch_bot_task_definition(bot_pod_spec_type)

        config_error = self._validate_config(task_definition)
        if config_error:
            logger.error("Cannot launch bot %s via ECS: %s", bot_id, config_error)
            return {"created": False, "status": "Error", "error": config_error}

        container_override = {
            "name": self.container_name,
            "command": ["python", "manage.py", "run_bot", "--botid", str(bot_id)],
            # The rest of the bot's env (DB, Redis, secrets) comes from the task
            # definition itself, mirroring the K8s envFrom configmap+secret.
            "environment": [{"name": "IS_A_BOT_POD", "value": "true"}],
        }

        run_task_kwargs = {
            "cluster": self.cluster,
            "taskDefinition": task_definition,
            "count": 1,
            "startedBy": self.started_by(bot_id),
            "networkConfiguration": self._network_configuration(),
            "overrides": {"containerOverrides": [container_override]},
            "enableExecuteCommand": self.enable_execute_command,
            "tags": [
                {"key": "bot-id", "value": str(bot_id)},
                {"key": "managed-by", "value": "attendee"},
            ],
            "propagateTags": "TASK_DEFINITION",
        }
        # Capacity provider and launchType are mutually exclusive in RunTask.
        if self.capacity_provider:
            run_task_kwargs["capacityProviderStrategy"] = [{"capacityProvider": self.capacity_provider, "weight": 1}]
        else:
            run_task_kwargs["launchType"] = self.launch_type

        try:
            response = self._run_task_with_retry(run_task_kwargs, f"bot task for bot {bot_id}")
        except ClientError as exc:
            logger.error("ECS RunTask failed for bot %s: %s", bot_id, exc)
            return {"created": False, "status": "Error", "error": str(exc)}

        tasks = response.get("tasks", [])
        failures = response.get("failures", [])
        if not tasks:
            logger.error("ECS RunTask placed no task for bot %s: %s", bot_id, failures)
            return {"created": False, "status": "Error", "error": str(failures)}

        task = tasks[0]
        task_arn = task.get("taskArn")
        logger.info("Bot %s launched as ECS task %s", bot_id, task_arn)
        return {
            "created": True,
            "name": task_arn,
            "task_arn": task_arn,
            "status": task.get("lastStatus"),
            "task_definition": task_definition,
        }

    def _validate_config(self, task_definition: Optional[str]) -> Optional[str]:
        missing = []
        if not self.cluster:
            missing.append("BOT_ECS_CLUSTER")
        if not task_definition:
            missing.append("BOT_TASK_DEFINITION")
        if not self.subnets:
            missing.append("BOT_TASK_SUBNETS")
        if not self.security_groups:
            missing.append("BOT_TASK_SECURITY_GROUPS")
        if missing:
            return f"missing required environment variables: {', '.join(missing)}"
        return None

    def find_bot_task_arns(self, bot_id: int) -> List[str]:
        """Return ARNs of running tasks for a bot (matched on the startedBy tag)."""
        arns: List[str] = []
        paginator = self.ecs.get_paginator("list_tasks")
        for page in paginator.paginate(cluster=self.cluster, startedBy=self.started_by(bot_id)):
            arns.extend(page.get("taskArns", []))
        return arns

    def stop_bot_task(self, bot_id: int, reason: str = "Stopped by attendee") -> Dict:
        """Stop any running task(s) for a bot. The ECS analogue of delete_bot_pod."""
        arns = self.find_bot_task_arns(bot_id)
        if not arns:
            return {"stopped": True, "task_arns": []}
        for arn in arns:
            try:
                self.ecs.stop_task(cluster=self.cluster, task=arn, reason=reason)
            except ClientError as exc:
                logger.error("Failed to stop ECS task %s for bot %s: %s", arn, bot_id, exc)
                return {"stopped": False, "task_arns": arns, "error": str(exc)}
        return {"stopped": True, "task_arns": arns}

# fmt: on
