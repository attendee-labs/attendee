import copy
import itertools
import os
import shutil
import signal
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import jsonpatch
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from kubernetes import client

from bots.bot_pod_creator import BotRunnerPodAssigner
from bots.bot_pod_creator.bot_pod_creator import POD_LABELS_VOLUME_MOUNT_PATH
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

    def list_namespaced_pod(self, namespace, label_selector=None):
        required_labels = self._parse_label_selector(label_selector)
        items = []
        for (pod_namespace, _), pod in self.pods.items():
            if pod_namespace != namespace:
                continue
            labels = pod["metadata"]["labels"]
            if all(labels.get(key) == value for key, value in required_labels.items()):
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
    def _to_pod(pod):
        metadata = pod["metadata"]
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=metadata["name"],
                namespace=metadata.get("namespace"),
                labels=dict(metadata.get("labels") or {}),
                uid=metadata.get("uid"),
                resource_version=metadata.get("resourceVersion"),
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

    def test_retries_on_another_runner_when_claimed_runner_is_taken_concurrently(self):
        self._add_runner_pod("bot-runner-a")
        self._add_runner_pod("bot-runner-b")
        stolen_pod_names = []

        def another_assigner_claims_pod_first(pod_name):
            stolen_pod_names.append(pod_name)
            self.fake_v1.labels(pod_name)["assigned-bot-id"] = "bot_someoneelse"

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
            self.fake_v1.labels(pod_name)["assigned-bot-id"] = "bot_someoneelse"

        self.fake_v1.before_patch = another_assigner_claims_pod_first

        launch_bot(self.bot)

        self.assertEqual(self.fake_v1.labels("bot-runner-a")["assigned-bot-id"], "bot_someoneelse")
        self.assertEqual(self._pods_assigned_to(self.bot.object_id), [self.bot.k8s_pod_name()])

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
            claimed = BotRunnerPodAssigner()._claim_pod_if_unassigned(pod_name, self.bot.object_id)
            self.assertTrue(claimed)

        self.fake_v1.before_delete = stale_runner_gets_assigned_before_delete

        self._maintain_pool(desired_count=0)

        # The delete precondition on resourceVersion failed, so the now-busy pod survives
        self.assertEqual(self.fake_v1.pod_names(), ["bot-runner-stale"])
        self.assertEqual(self.fake_v1.labels("bot-runner-stale")["assigned-bot-id"], self.bot.object_id)

    def test_pool_manager_does_nothing_when_pool_is_full(self):
        self._maintain_pool(desired_count=3)
        pod_names = self.fake_v1.pod_names()

        self._maintain_pool(desired_count=3)

        self.assertEqual(self.fake_v1.pod_names(), pod_names)
