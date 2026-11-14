import copy
import itertools
import os
import shutil
import signal
import tempfile
from collections import deque
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import jsonpatch
import redis
from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from kubernetes import client

from bots.bot_pod_creator import BotRunnerPodAssigner
from bots.bot_pod_creator.bot_pod_creator import POD_LABELS_VOLUME_MOUNT_PATH
from bots.bot_pod_creator.bot_runner_pod_assigner import BOT_RUNNER_POOL_LAUNCH_COUNTS_REDIS_KEY, get_bot_runner_pool_launch_counts
from bots.k8s_utils import find_bot_pod
from bots.launch_bot_utils import launch_bot
from bots.management.commands.create_bot_runner_pods import Command as CreateBotRunnerPodsCommand
from bots.models import Bot, Organization, Project

NAMESPACE = "attendee-test"
WEBPAGE_STREAMER_NAMESPACE = "attendee-webpage-streamer-test"
APP_VERSION = "abc123-1700000000"


class FakeCoreV1Api:
    """
    In-memory stand-in for kubernetes.client.CoreV1Api.

    Pods are stored as serialized dicts, the same shape the real API server receives. Supports
    equality-based label selectors, RFC 6902 patches (including "test" ops, which fail with 422 like
    the real API server) and delete preconditions on uid / resourceVersion (which fail with 409).
    """

    def __init__(self):
        self.pods = {}
        self.services = []
        self._uid_counter = itertools.count(1)
        self.before_patch = None
        self.before_delete = None

    def create_namespaced_pod(self, namespace, body):
        if not isinstance(body, dict):
            body = client.ApiClient().sanitize_for_serialization(body)
        body = copy.deepcopy(body)
        name = body["metadata"]["name"]
        if (namespace, name) in self.pods:
            raise client.ApiException(status=409, reason="AlreadyExists")
        body["metadata"]["namespace"] = namespace
        body["metadata"]["uid"] = f"uid-{next(self._uid_counter)}"
        body["metadata"]["resourceVersion"] = "1"
        body["metadata"].setdefault("labels", {})
        body["status"] = {"phase": "Pending"}
        self.pods[(namespace, name)] = body
        return self._to_pod(body)

    def read_namespaced_pod(self, name, namespace):
        pod = self.pods.get((namespace, name))
        if pod is None:
            raise client.ApiException(status=404, reason="Not Found")
        return self._to_pod(pod)

    def list_namespaced_pod(self, namespace, label_selector=None, field_selector=None):
        required_labels = self._parse_label_selector(label_selector)
        required_fields = self._parse_label_selector(field_selector)
        items = []
        for (pod_namespace, _), pod in self.pods.items():
            if pod_namespace != namespace:
                continue
            labels = pod["metadata"]["labels"]
            if not all(labels.get(key) == value for key, value in required_labels.items()):
                continue
            if not all(self._get_field(pod, key) == value for key, value in required_fields.items()):
                continue
            items.append(self._to_pod(pod))
        return SimpleNamespace(items=items)

    def patch_namespaced_pod(self, name, namespace, body):
        if self.before_patch:
            hook, self.before_patch = self.before_patch, None
            hook(name)

        pod = self.pods.get((namespace, name))
        if pod is None:
            raise client.ApiException(status=404, reason="Not Found")
        try:
            patched = jsonpatch.apply_patch(pod, body)
        except jsonpatch.JsonPatchTestFailed as e:
            raise client.ApiException(status=422, reason=f"Unprocessable Entity: {e}")
        patched["metadata"]["resourceVersion"] = str(int(pod["metadata"]["resourceVersion"]) + 1)
        self.pods[(namespace, name)] = patched
        return self._to_pod(patched)

    def delete_namespaced_pod(self, name, namespace, body=None, grace_period_seconds=None):
        if self.before_delete:
            hook, self.before_delete = self.before_delete, None
            hook(name)

        pod = self.pods.get((namespace, name))
        if pod is None:
            raise client.ApiException(status=404, reason="Not Found")
        preconditions = getattr(body, "preconditions", None)
        if preconditions is not None:
            if preconditions.uid is not None and preconditions.uid != pod["metadata"]["uid"]:
                raise client.ApiException(status=409, reason="Conflict")
            if preconditions.resource_version is not None and preconditions.resource_version != pod["metadata"]["resourceVersion"]:
                raise client.ApiException(status=409, reason="Conflict")
        del self.pods[(namespace, name)]

    def create_namespaced_service(self, namespace, body):
        self.services.append((namespace, body))
        return body

    # Test helpers that simulate what the scheduler / kubelet would do

    def set_pod_phase(self, name, phase, namespace=NAMESPACE):
        self.pods[(namespace, name)]["status"]["phase"] = phase

    def set_all_pod_phases(self, phase, namespace=NAMESPACE):
        for pod_namespace, name in list(self.pods):
            if pod_namespace == namespace:
                self.set_pod_phase(name, phase, namespace)

    def mark_pod_terminating(self, name, namespace=NAMESPACE):
        """Simulate a graceful delete in progress: the pod keeps its phase but gets a deletionTimestamp."""
        metadata = self.pods[(namespace, name)]["metadata"]
        metadata["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        metadata["resourceVersion"] = str(int(metadata["resourceVersion"]) + 1)

    def set_label(self, name, key, value, namespace=NAMESPACE):
        """Simulate another client changing a label, which bumps resourceVersion like any real write."""
        metadata = self.pods[(namespace, name)]["metadata"]
        metadata["labels"][key] = value
        metadata["resourceVersion"] = str(int(metadata["resourceVersion"]) + 1)

    def labels(self, name, namespace=NAMESPACE):
        return self.pods[(namespace, name)]["metadata"]["labels"]

    def raw_pod(self, name, namespace=NAMESPACE):
        return self.pods[(namespace, name)]

    def pod_names(self, namespace=NAMESPACE):
        return sorted(name for pod_namespace, name in self.pods if pod_namespace == namespace)

    def write_downward_api_labels_file(self, name, labels_file_path, namespace=NAMESPACE):
        """Write the pod's labels in the same format the kubelet uses for downward API volumes."""
        labels = self.labels(name, namespace)
        with open(labels_file_path, "w") as f:
            for key in sorted(labels):
                f.write(f'{key}="{labels[key]}"\n')

    @staticmethod
    def _parse_label_selector(label_selector):
        if not label_selector:
            return {}
        return dict(term.split("=", 1) for term in label_selector.split(","))

    @staticmethod
    def _get_field(pod, field_path):
        value = pod
        for part in field_path.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    @staticmethod
    def _to_pod(pod):
        metadata = pod["metadata"]
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=metadata["name"],
                namespace=metadata.get("namespace"),
                labels=dict(metadata.get("labels") or {}),
                uid=metadata.get("uid"),
                resource_version=metadata.get("resourceVersion"),
                deletion_timestamp=metadata.get("deletionTimestamp"),
            ),
            status=SimpleNamespace(phase=pod["status"]["phase"]),
        )


@override_settings(
    BOT_POD_NAMESPACE=NAMESPACE,
    WEBPAGE_STREAMER_POD_NAMESPACE=WEBPAGE_STREAMER_NAMESPACE,
    CUSTOM_BOT_POD_SPEC_TYPES=[],
)
class BotRunnerPoolEndToEndTest(TestCase):
    """
    Exercises the bot runner pool flow end to end against a fake Kubernetes API:
    the pool manager creates runner pods, launch_bot assigns a bot to one of them by
    patching its assigned-bot-id label, and the runner pod picks up the assignment from
    its downward API labels file and runs the bot.
    """

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://meet.google.com/abc-defg-hij")

        self.fake_v1 = FakeCoreV1Api()
        self.labels_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.labels_dir)
        self.labels_file_path = os.path.join(self.labels_dir, "labels")

        # Launch counts are running totals in Redis, so start every test from zero
        self.redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
        self.addCleanup(self.redis_client.close)
        self.redis_client.delete(BOT_RUNNER_POOL_LAUNCH_COUNTS_REDIS_KEY)
        self.addCleanup(self.redis_client.delete, BOT_RUNNER_POOL_LAUNCH_COUNTS_REDIS_KEY)

        env_patcher = patch.dict(
            os.environ,
            {
                "CUBER_RELEASE_VERSION": APP_VERSION,
                "LAUNCH_BOT_METHOD": "kubernetes",
                "USE_BOT_RUNNER_POOL": "true",
                "BOT_POD_SPEC_DEFAULT": "",
                "BOT_POD_SPEC_SCHEDULED": "",
            },
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        for patcher in (
            patch("kubernetes.config.load_incluster_config"),
            patch("kubernetes.client.CoreV1Api", return_value=self.fake_v1),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        sleep_patcher = patch("time.sleep")
        self.mock_sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        run_bot_patcher = patch("bots.management.commands.wait_for_bot_runner_assignment.run_bot")
        self.mock_run_bot = run_bot_patcher.start()
        self.addCleanup(run_bot_patcher.stop)

        # wait_for_bot_runner_assignment installs its own SIGINT / SIGTERM handlers
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))

    def _maintain_pool(self, desired_count):
        CreateBotRunnerPodsCommand()._maintain_bot_runner_pool(desired_count)

    def _add_runner_pod(self, name, phase="Running", version=APP_VERSION, assigned_bot_id="none"):
        self.fake_v1.create_namespaced_pod(
            namespace=NAMESPACE,
            body={
                "metadata": {
                    "name": name,
                    "labels": {
                        "is-bot-runner": "true",
                        "assigned-bot-id": assigned_bot_id,
                        "app.kubernetes.io/version": version,
                    },
                },
                "spec": {},
            },
        )
        self.fake_v1.set_pod_phase(name, phase)

    def _pods_assigned_to(self, bot_object_id):
        return [name for name in self.fake_v1.pod_names() if self.fake_v1.labels(name).get("assigned-bot-id") == bot_object_id]

    def _unassigned_runner_pods(self):
        return [name for name in self.fake_v1.pod_names() if self.fake_v1.labels(name).get("is-bot-runner") == "true" and self.fake_v1.labels(name).get("assigned-bot-id") == "none"]

    def _run_bot_runner(self, pod_name):
        """Simulate the kubelet projecting the pod's labels, then run the runner command inside the pod."""
        self.fake_v1.write_downward_api_labels_file(pod_name, self.labels_file_path)
        call_command("wait_for_bot_runner_assignment", labels_file=self.labels_file_path, poll_interval=0)

    def _run_idle_bot_runner_until_terminated(self, pod_name):
        """Run the runner command on an unassigned pod, sending it SIGTERM while it polls (like k8s deleting the pod)."""
        self.mock_sleep.side_effect = lambda _seconds: os.kill(os.getpid(), signal.SIGTERM)
        try:
            self._run_bot_runner(pod_name)
        finally:
            self.mock_sleep.side_effect = None

    def test_full_lifecycle_from_pool_creation_to_bot_running(self):
        self._maintain_pool(desired_count=2)

        runner_pod_names = self.fake_v1.pod_names()
        self.assertEqual(len(runner_pod_names), 2)
        for pod_name in runner_pod_names:
            self.assertTrue(pod_name.startswith("bot-runner-"))
            labels = self.fake_v1.labels(pod_name)
            self.assertEqual(labels["is-bot-runner"], "true")
            self.assertEqual(labels["assigned-bot-id"], "none")
            self.assertEqual(labels["app.kubernetes.io/version"], APP_VERSION)

            pod_spec = self.fake_v1.raw_pod(pod_name)["spec"]
            container = pod_spec["containers"][0]
            self.assertEqual(container["args"], ["python", "manage.py", "wait_for_bot_runner_assignment"])
            self.assertIn(
                {"name": "pod-labels", "mountPath": POD_LABELS_VOLUME_MOUNT_PATH, "readOnly": True},
                container["volumeMounts"],
            )
            labels_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "pod-labels")
            self.assertEqual(
                labels_volume["downwardAPI"]["items"],
                [{"path": "labels", "fieldRef": {"fieldPath": "metadata.labels"}}],
            )

        # Runner pods start idle, waiting for an assignment
        self.fake_v1.set_all_pod_phases("Running")
        for pod_name in runner_pod_names:
            self._run_idle_bot_runner_until_terminated(pod_name)
        self.mock_run_bot.run.assert_not_called()
        self.mock_sleep.reset_mock()

        launch_bot(self.bot)

        # The bot was assigned to an existing runner instead of getting its own pod
        self.assertEqual(self.fake_v1.pod_names(), runner_pod_names)
        assigned_pod_names = self._pods_assigned_to(self.bot.object_id)
        self.assertEqual(len(assigned_pod_names), 1)
        assigned_pod_name = assigned_pod_names[0]
        self.assertIn(assigned_pod_name, runner_pod_names)

        self._run_bot_runner(assigned_pod_name)
        self.mock_run_bot.run.assert_called_once_with(self.bot.id)

        found_pod = find_bot_pod(self.fake_v1, NAMESPACE, self.bot)
        self.assertEqual(found_pod.metadata.name, assigned_pod_name)

        # The assigned runner no longer counts towards the pool, so the manager replaces it
        self._maintain_pool(desired_count=2)
        self.assertEqual(len(self.fake_v1.pod_names()), 3)
        self.assertEqual(len(self._unassigned_runner_pods()), 2)
        self.assertIn(assigned_pod_name, self.fake_v1.pod_names())

    def test_runner_picks_up_assignment_made_while_it_is_waiting(self):
        self._maintain_pool(desired_count=1)
        self.fake_v1.set_all_pod_phases("Running")
        runner_pod_name = self.fake_v1.pod_names()[0]
        self.fake_v1.write_downward_api_labels_file(runner_pod_name, self.labels_file_path)

        def assign_bot_and_propagate_labels(_seconds):
            launch_bot(self.bot)
            self.fake_v1.write_downward_api_labels_file(runner_pod_name, self.labels_file_path)

        # The runner sees "none" on its first read, sleeps, and the bot gets launched during that sleep
        self.mock_sleep.side_effect = assign_bot_and_propagate_labels

        call_command("wait_for_bot_runner_assignment", labels_file=self.labels_file_path, poll_interval=0)

        self.mock_run_bot.run.assert_called_once_with(self.bot.id)
        self.assertEqual(self.fake_v1.labels(runner_pod_name)["assigned-bot-id"], self.bot.object_id)

    def test_runner_assigned_to_unknown_bot_does_not_run_anything(self):
        self._add_runner_pod("bot-runner-orphan", assigned_bot_id="bot_doesnotexist")

        self._run_bot_runner("bot-runner-orphan")

        self.mock_run_bot.run.assert_not_called()

    def test_multiple_bots_are_assigned_to_distinct_runners(self):
        other_bot = Bot.objects.create(project=self.project, meeting_url="https://meet.google.com/xyz-wxyz-xyz")
        self._maintain_pool(desired_count=2)
        self.fake_v1.set_all_pod_phases("Running")

        launch_bot(self.bot)
        launch_bot(other_bot)

        bot_pods = self._pods_assigned_to(self.bot.object_id)
        other_bot_pods = self._pods_assigned_to(other_bot.object_id)
        self.assertEqual(len(bot_pods), 1)
        self.assertEqual(len(other_bot_pods), 1)
        self.assertNotEqual(bot_pods[0], other_bot_pods[0])
        self.assertEqual(len(self.fake_v1.pod_names()), 2)

        self._run_bot_runner(bot_pods[0])
        self._run_bot_runner(other_bot_pods[0])
        self.assertEqual([c.args for c in self.mock_run_bot.run.call_args_list], [(self.bot.id,), (other_bot.id,)])

    def test_falls_back_to_dedicated_pod_when_pool_is_empty(self):
        launch_bot(self.bot)

        self.assertEqual(self.fake_v1.pod_names(), [self.bot.k8s_pod_name()])
        labels = self.fake_v1.labels(self.bot.k8s_pod_name())
        self.assertEqual(labels["assigned-bot-id"], self.bot.object_id)
        self.assertNotIn("is-bot-runner", labels)
        container = self.fake_v1.raw_pod(self.bot.k8s_pod_name())["spec"]["containers"][0]
        self.assertEqual(container["args"], self.bot.k8s_pod_cmd())

        self.assertEqual(find_bot_pod(self.fake_v1, NAMESPACE, self.bot).metadata.name, self.bot.k8s_pod_name())

    def test_falls_back_to_dedicated_pod_when_runners_are_still_pending(self):
        self._maintain_pool(desired_count=2)
        runner_pod_names = self.fake_v1.pod_names()

        launch_bot(self.bot)

        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])
        for pod_name in runner_pod_names:
            self.assertEqual(self.fake_v1.labels(pod_name)["assigned-bot-id"], "none")

    def test_ignores_runner_pods_from_other_versions(self):
        self._add_runner_pod("bot-runner-old", version="old-1600000000")

        launch_bot(self.bot)

        self.assertEqual(self.fake_v1.labels("bot-runner-old")["assigned-bot-id"], "none")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])

    def test_ignores_runner_pods_that_are_terminating(self):
        self._add_runner_pod("bot-runner-terminating")
        self.fake_v1.mark_pod_terminating("bot-runner-terminating")

        launch_bot(self.bot)

        self.assertEqual(self.fake_v1.labels("bot-runner-terminating")["assigned-bot-id"], "none")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])

    def test_assigns_to_healthy_runner_when_another_is_terminating(self):
        self._add_runner_pod("bot-runner-terminating")
        self._add_runner_pod("bot-runner-healthy")
        self.fake_v1.mark_pod_terminating("bot-runner-terminating")

        launch_bot(self.bot)

        self.assertEqual(self.fake_v1.labels("bot-runner-terminating")["assigned-bot-id"], "none")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), ["bot-runner-healthy"])

    def test_does_not_assign_to_runner_that_starts_terminating_after_listing(self):
        self._add_runner_pod("bot-runner-a")

        def runner_starts_terminating_before_claim(pod_name):
            self.fake_v1.mark_pod_terminating(pod_name)

        self.fake_v1.before_patch = runner_starts_terminating_before_claim

        launch_bot(self.bot)

        # The resourceVersion "test" op rejected the stale claim, and the retry's listing skips the terminating pod
        self.assertEqual(self.fake_v1.labels("bot-runner-a")["assigned-bot-id"], "none")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])

    def test_retries_on_another_runner_when_claimed_runner_is_taken_concurrently(self):
        self._add_runner_pod("bot-runner-a")
        self._add_runner_pod("bot-runner-b")
        stolen_pod_names = []

        def another_assigner_claims_pod_first(pod_name):
            stolen_pod_names.append(pod_name)
            self.fake_v1.set_label(pod_name, "assigned-bot-id", "bot_someoneelse")

        self.fake_v1.before_patch = another_assigner_claims_pod_first

        launch_bot(self.bot)

        self.assertEqual(len(stolen_pod_names), 1)
        stolen_pod_name = stolen_pod_names[0]
        remaining_pod_name = "bot-runner-b" if stolen_pod_name == "bot-runner-a" else "bot-runner-a"

        # The JSON patch "test" op kept us from overwriting the other bot's assignment
        self.assertEqual(self.fake_v1.labels(stolen_pod_name)["assigned-bot-id"], "bot_someoneelse")
        self.assertEqual(self.fake_v1.labels(remaining_pod_name)["assigned-bot-id"], self.bot.object_id)
        self.mock_sleep.assert_called_once_with(1)
        self.assertEqual(self.fake_v1.pod_names(), ["bot-runner-a", "bot-runner-b"])

    def test_falls_back_to_dedicated_pod_when_only_runner_is_taken_concurrently(self):
        self._add_runner_pod("bot-runner-a")

        def another_assigner_claims_pod_first(pod_name):
            self.fake_v1.set_label(pod_name, "assigned-bot-id", "bot_someoneelse")

        self.fake_v1.before_patch = another_assigner_claims_pod_first

        launch_bot(self.bot)

        self.assertEqual(self.fake_v1.labels("bot-runner-a")["assigned-bot-id"], "bot_someoneelse")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])

    def _simulate_gateway_timeouts_on_patch(self, outcomes):
        """
        Make the client see a 504 for some patch calls. Each entry in outcomes applies to one call, in order:
        - "ok": the patch is applied and the response is returned
        - "committed": the patch is applied, but the client gets a 504
        - "in_flight": the client gets a 504 before the API server has applied the patch. The request is
          appended to the returned list so the test can let it reach the API server later.
        Calls beyond the list behave normally.
        """
        real_patch = self.fake_v1.patch_namespaced_pod
        outcomes = iter(outcomes)
        in_flight_patches = []

        def patch_namespaced_pod(name, namespace, body):
            outcome = next(outcomes, "ok")
            if outcome == "in_flight":
                in_flight_patches.append({"name": name, "namespace": namespace, "body": body})
                raise client.ApiException(status=504, reason="Gateway Timeout")
            result = real_patch(name=name, namespace=namespace, body=body)
            if outcome == "committed":
                raise client.ApiException(status=504, reason="Gateway Timeout")
            return result

        return patch.object(self.fake_v1, "patch_namespaced_pod", side_effect=patch_namespaced_pod), in_flight_patches

    def test_does_not_claim_second_runner_when_claim_committed_but_reported_failure(self):
        self._add_runner_pod("bot-runner-a")
        self._add_runner_pod("bot-runner-b")
        patcher, _ = self._simulate_gateway_timeouts_on_patch(["committed"])

        with patcher:
            launch_bot(self.bot)

        self.assertEqual(len(self._pods_assigned_to(self.bot.object_id)), 1)
        self.assertEqual(len(self._unassigned_runner_pods()), 1)
        self.assertEqual(self.fake_v1.pod_names(), ["bot-runner-a", "bot-runner-b"])
        self.mock_sleep.assert_not_called()

    def test_resends_claim_to_same_runner_when_claim_is_still_in_flight(self):
        self._add_runner_pod("bot-runner-a")
        self._add_runner_pod("bot-runner-b")
        patcher, in_flight_patches = self._simulate_gateway_timeouts_on_patch(["in_flight"])

        with patcher:
            launch_bot(self.bot)

        claimed_pod_name = in_flight_patches[0]["name"]
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [claimed_pod_name])

        # The original request reaching the API server late is rejected by the resourceVersion test
        with self.assertRaises(client.ApiException) as context:
            self.fake_v1.patch_namespaced_pod(**in_flight_patches[0])
        self.assertEqual(context.exception.status, 422)
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [claimed_pod_name])
        self.assertEqual(self.fake_v1.pod_names(), ["bot-runner-a", "bot-runner-b"])
        self.mock_sleep.assert_not_called()

    def test_does_not_launch_elsewhere_when_claim_outcome_stays_unknown(self):
        self._add_runner_pod("bot-runner-a")
        self._add_runner_pod("bot-runner-b")
        patcher, in_flight_patches = self._simulate_gateway_timeouts_on_patch(["in_flight", "in_flight"])

        with patcher:
            launch_bot(self.bot)

        # Neither another runner nor a dedicated pod was used
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [])
        self.assertEqual(self.fake_v1.pod_names(), ["bot-runner-a", "bot-runner-b"])

        # Both requests went to the same runner, so whichever lands, the bot runs on exactly one pod
        claimed_pod_name = in_flight_patches[0]["name"]
        self.assertEqual(in_flight_patches[1]["name"], claimed_pod_name)
        self.fake_v1.patch_namespaced_pod(**in_flight_patches[1])
        with self.assertRaises(client.ApiException):
            self.fake_v1.patch_namespaced_pod(**in_flight_patches[0])
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [claimed_pod_name])

    def test_does_not_launch_elsewhere_when_claim_cannot_be_verified(self):
        self._add_runner_pod("bot-runner-a")
        self._add_runner_pod("bot-runner-b")
        patcher, in_flight_patches = self._simulate_gateway_timeouts_on_patch(["in_flight"])

        with patcher, patch.object(self.fake_v1, "read_namespaced_pod", side_effect=client.ApiException(status=500)):
            launch_bot(self.bot)

        self.assertEqual(len(in_flight_patches), 1)
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [])
        self.assertEqual(self.fake_v1.pod_names(), ["bot-runner-a", "bot-runner-b"])

    def test_retries_on_another_runner_when_retry_claim_committed_but_reported_failure(self):
        self._add_runner_pod("bot-runner-a")
        self._add_runner_pod("bot-runner-b")
        stolen_pod_names = []

        def another_assigner_claims_pod_first(pod_name):
            stolen_pod_names.append(pod_name)
            self.fake_v1.set_label(pod_name, "assigned-bot-id", "bot_someoneelse")

        self.fake_v1.before_patch = another_assigner_claims_pod_first
        # The first patch is rejected by the "test" op, the retry's patch is committed but reported as failed
        patcher, _ = self._simulate_gateway_timeouts_on_patch(["ok", "committed"])

        with patcher:
            launch_bot(self.bot)

        remaining_pod_name = "bot-runner-b" if stolen_pod_names[0] == "bot-runner-a" else "bot-runner-a"
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [remaining_pod_name])
        self.assertEqual(self.fake_v1.pod_names(), ["bot-runner-a", "bot-runner-b"])
        self.mock_sleep.assert_called_once_with(1)

    def test_scheduled_bot_does_not_use_runner_pool(self):
        self._add_runner_pod("bot-runner-a")
        self.bot.join_at = timezone.now() + timedelta(minutes=10)
        self.bot.save()

        launch_bot(self.bot)

        self.assertEqual(self.fake_v1.labels("bot-runner-a")["assigned-bot-id"], "none")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])

    def test_bot_with_webpage_streamer_does_not_use_runner_pool(self):
        self._add_runner_pod("bot-runner-a")
        self.bot.settings = {"voice_agent_settings": {"url": "https://example.com/agent", "reserve_resources": True}}
        self.bot.save()

        launch_bot(self.bot)

        self.assertEqual(self.fake_v1.labels("bot-runner-a")["assigned-bot-id"], "none")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])
        self.assertEqual(self.fake_v1.pod_names(WEBPAGE_STREAMER_NAMESPACE), [f"{self.bot.k8s_pod_name()}-webpage-streamer"])
        self.assertEqual(len(self.fake_v1.services), 1)

    def test_runner_pool_disabled_creates_dedicated_pod(self):
        self._add_runner_pod("bot-runner-a")

        with patch.dict(os.environ, {"USE_BOT_RUNNER_POOL": "false"}):
            launch_bot(self.bot)

        self.assertEqual(self.fake_v1.labels("bot-runner-a")["assigned-bot-id"], "none")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])

    def test_pool_manager_deletes_stale_unassigned_runners_but_keeps_assigned_ones(self):
        self._add_runner_pod("bot-runner-stale", version="old-1600000000")
        self._add_runner_pod("bot-runner-old-busy", version="old-1600000000", assigned_bot_id="bot_inprogress")
        self._add_runner_pod("bot-runner-current")

        self._maintain_pool(desired_count=2)

        pod_names = self.fake_v1.pod_names()
        self.assertNotIn("bot-runner-stale", pod_names)
        self.assertIn("bot-runner-old-busy", pod_names)
        self.assertIn("bot-runner-current", pod_names)
        unassigned_current_version = [name for name in self._unassigned_runner_pods() if self.fake_v1.labels(name)["app.kubernetes.io/version"] == APP_VERSION]
        self.assertEqual(len(unassigned_current_version), 2)

    def test_pool_manager_does_not_delete_stale_runner_assigned_after_listing(self):
        self._add_runner_pod("bot-runner-stale", version="old-1600000000")

        def stale_runner_gets_assigned_before_delete(pod_name):
            resource_version = self.fake_v1.raw_pod(pod_name)["metadata"]["resourceVersion"]
            claimed = BotRunnerPodAssigner()._claim_pod_if_unassigned(pod_name, resource_version, self.bot.object_id)
            self.assertTrue(claimed)

        self.fake_v1.before_delete = stale_runner_gets_assigned_before_delete

        self._maintain_pool(desired_count=0)

        # The delete precondition on resourceVersion failed, so the now-busy pod survives
        self.assertEqual(self.fake_v1.pod_names(), ["bot-runner-stale"])
        self.assertEqual(self.fake_v1.labels("bot-runner-stale")["assigned-bot-id"], self.bot.object_id)

    def test_pool_manager_does_not_count_finished_runners(self):
        for i in range(3):
            self._add_runner_pod(f"bot-runner-failed-{i}", phase="Failed")
        self._add_runner_pod("bot-runner-succeeded", phase="Succeeded")
        finished_pod_names = self.fake_v1.pod_names()

        self._maintain_pool(desired_count=3)

        # Finished runners are kept around for debugging, but new runners are created alongside them
        new_pod_names = [name for name in self.fake_v1.pod_names() if name not in finished_pod_names]
        self.assertEqual(len(new_pod_names), 3)
        self.assertTrue(set(finished_pod_names).issubset(self.fake_v1.pod_names()))

        for name in new_pod_names:
            self.fake_v1.set_pod_phase(name, "Running")
        launch_bot(self.bot)
        assigned_pod_names = self._pods_assigned_to(self.bot.object_id)
        self.assertEqual(len(assigned_pod_names), 1)
        self.assertIn(assigned_pod_names[0], new_pod_names)

    def test_pool_manager_does_not_count_terminating_runners(self):
        self._add_runner_pod("bot-runner-terminating")
        self.fake_v1.mark_pod_terminating("bot-runner-terminating")

        self._maintain_pool(desired_count=1)

        self.assertIn("bot-runner-terminating", self.fake_v1.pod_names())
        self.assertEqual(len(self._unassigned_runner_pods()), 2)

    def test_pool_manager_does_not_create_pods_when_listing_fails(self):
        with patch.object(self.fake_v1, "list_namespaced_pod", side_effect=client.ApiException(status=500)):
            self._maintain_pool(desired_count=3)

        self.assertEqual(self.fake_v1.pod_names(), [])

    def test_pool_manager_does_nothing_when_pool_is_full(self):
        self._maintain_pool(desired_count=3)
        pod_names = self.fake_v1.pod_names()

        self._maintain_pool(desired_count=3)

        self.assertEqual(self.fake_v1.pod_names(), pod_names)

    def test_pool_manager_shrinks_pool_deleting_pending_runners_first(self):
        self._add_runner_pod("bot-runner-running-a")
        self._add_runner_pod("bot-runner-running-b")
        self._add_runner_pod("bot-runner-pending", phase="Pending")
        self._add_runner_pod("bot-runner-busy", assigned_bot_id="bot_inprogress")

        self._maintain_pool(desired_count=1)

        self.assertNotIn("bot-runner-pending", self.fake_v1.pod_names())
        self.assertIn("bot-runner-busy", self.fake_v1.pod_names())
        self.assertEqual(len(self._unassigned_runner_pods()), 1)

    def test_pool_manager_does_not_delete_runner_claimed_while_shrinking(self):
        self._add_runner_pod("bot-runner-a")

        def runner_gets_claimed_before_delete(pod_name):
            resource_version = self.fake_v1.raw_pod(pod_name)["metadata"]["resourceVersion"]
            self.assertTrue(BotRunnerPodAssigner()._claim_pod_if_unassigned(pod_name, resource_version, self.bot.object_id))

        self.fake_v1.before_delete = runner_gets_claimed_before_delete

        self._maintain_pool(desired_count=0)

        self.assertEqual(self._pods_assigned_to(self.bot.object_id), ["bot-runner-a"])

    def _pool_sizer(self, min_size=1, max_size=10, peak_launch_multiplier=2, peak_launch_lookback_cycles=3, hold_max_size=False):
        command = CreateBotRunnerPodsCommand()
        command.min_size = min_size
        command.max_size = max_size
        command.peak_launch_multiplier = peak_launch_multiplier
        command._launches_per_cycle = deque(maxlen=peak_launch_lookback_cycles)
        command._prev_launch_counts = None
        command._hold_max_size_until = float("inf") if hold_max_size else 0
        return command

    def _launch_bots(self, count):
        for _ in range(count):
            launch_bot(Bot.objects.create(project=self.project, meeting_url="https://meet.google.com/abc-defg-hij"))

    def test_pool_size_grows_with_recent_launches_and_shrinks_after_lookback(self):
        command = self._pool_sizer(peak_launch_lookback_cycles=3)

        self.assertEqual(command._desired_pool_size(), 1)

        # Only one launch gets a runner. The other two fall back to dedicated pods, but still count as demand.
        self._add_runner_pod("bot-runner-a")
        self._launch_bots(3)
        self.assertEqual(get_bot_runner_pool_launch_counts(), {"assigned": 1, "not_assigned": 2})
        self.assertEqual(command._desired_pool_size(), 3 * 2)

        # The busy cycle holds the size until it is older than the lookback
        self.assertEqual(command._desired_pool_size(), 6)
        self.assertEqual(command._desired_pool_size(), 6)
        self.assertEqual(command._desired_pool_size(), 1)

    def test_pool_size_is_clamped_to_max(self):
        command = self._pool_sizer(max_size=4)
        command._desired_pool_size()

        self._launch_bots(5)

        self.assertEqual(command._desired_pool_size(), 4)

    def test_pool_size_ignores_launches_from_before_the_manager_started(self):
        self._launch_bots(3)
        command = self._pool_sizer()

        self.assertEqual(command._desired_pool_size(), 1)

        self._launch_bots(1)
        self.assertEqual(command._desired_pool_size(), 2)

    def test_pool_size_counts_launches_after_launch_counts_are_lost(self):
        command = self._pool_sizer()
        self._launch_bots(3)
        command._desired_pool_size()

        self.redis_client.delete(BOT_RUNNER_POOL_LAUNCH_COUNTS_REDIS_KEY)
        self._launch_bots(2)

        self.assertEqual(command._desired_pool_size(), 4)

    def test_bot_is_still_launched_when_launch_cannot_be_recorded(self):
        self._add_runner_pod("bot-runner-a")

        with patch("bots.bot_pod_creator.bot_runner_pod_assigner._get_redis_client", side_effect=redis.ConnectionError("redis is down")):
            launch_bot(self.bot)

        self.assertEqual(self._pods_assigned_to(self.bot.object_id), ["bot-runner-a"])

    def test_pool_size_holds_max_size_after_startup(self):
        command = self._pool_sizer(max_size=5, hold_max_size=True)

        self.assertEqual(command._desired_pool_size(), 5)

        command._hold_max_size_until = 0
        self.assertEqual(command._desired_pool_size(), 1)

    def test_pool_size_is_max_when_launch_counts_cannot_be_read(self):
        command = self._pool_sizer(max_size=10)
        command._desired_pool_size()
        self._launch_bots(2)
        self.assertEqual(command._desired_pool_size(), 4)

        with patch("bots.management.commands.create_bot_runner_pods.get_bot_runner_pool_launch_counts", side_effect=redis.ConnectionError("redis is down")):
            self.assertEqual(command._desired_pool_size(), 10)

        # Once the counts can be read again, the pool goes back to being sized from the launches in the lookback
        self.assertEqual(command._desired_pool_size(), 4)

    def test_pool_size_is_max_when_launch_counts_have_never_been_read(self):
        command = self._pool_sizer(max_size=10)

        with patch("bots.management.commands.create_bot_runner_pods.get_bot_runner_pool_launch_counts", side_effect=redis.ConnectionError("redis is down")):
            self.assertEqual(command._desired_pool_size(), 10)
