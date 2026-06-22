import os
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from django.test import TestCase

from bots.bot_ecs_task_creator.bot_ecs_task_creator import (
    BotEcsTaskCreator,
    _split_csv,
    fetch_bot_task_definition,
    is_transient_ecs_failure,
)

BASE_ENV = {
    "BOT_ECS_CLUSTER": "attendee-bots",
    "BOT_TASK_DEFINITION": "attendee-bot",
    "BOT_TASK_SUBNETS": "subnet-aaa, subnet-bbb",
    "BOT_TASK_SECURITY_GROUPS": "sg-123",
}


class TestHelpers(TestCase):
    def test_split_csv(self):
        self.assertEqual(_split_csv("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(_split_csv(""), [])
        self.assertEqual(_split_csv(None), [])

    def test_fetch_bot_task_definition_default(self):
        with patch.dict(os.environ, {"BOT_TASK_DEFINITION": "default-td"}, clear=True):
            self.assertEqual(fetch_bot_task_definition(None), "default-td")
            self.assertEqual(fetch_bot_task_definition("DEFAULT"), "default-td")

    def test_fetch_bot_task_definition_spec_override(self):
        env = {"BOT_TASK_DEFINITION": "default-td", "BOT_TASK_DEFINITION_HIGHMEM": "highmem-td"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(fetch_bot_task_definition("HIGHMEM"), "highmem-td")
            # Unknown spec type falls back to the default.
            self.assertEqual(fetch_bot_task_definition("OTHER"), "default-td")

    def test_is_transient_ecs_failure(self):
        self.assertTrue(is_transient_ecs_failure([{"reason": "Capacity is unavailable at this time"}]))
        self.assertTrue(is_transient_ecs_failure([{"reason": "RESOURCE:MEMORY"}]))
        self.assertFalse(is_transient_ecs_failure([{"reason": "AccessDeniedException"}]))
        self.assertFalse(is_transient_ecs_failure([]))


class TestCreateBotTask(TestCase):
    def _creator_with_mock_ecs(self, env):
        # The task definition is resolved from the environment at create_bot_task
        # time, so keep the patched env active for the whole test (not just __init__).
        env_patcher = patch.dict(os.environ, env, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        mock_ecs = MagicMock()
        with patch("bots.bot_ecs_task_creator.bot_ecs_task_creator.boto3.client", return_value=mock_ecs):
            creator = BotEcsTaskCreator()
        return creator, mock_ecs

    def test_create_bot_task_success(self):
        creator, mock_ecs = self._creator_with_mock_ecs(BASE_ENV)
        mock_ecs.run_task.return_value = {"tasks": [{"taskArn": "arn:task/abc", "lastStatus": "PROVISIONING"}], "failures": []}

        result = creator.create_bot_task(bot_id=42)

        self.assertTrue(result["created"])
        self.assertEqual(result["task_arn"], "arn:task/abc")

        kwargs = mock_ecs.run_task.call_args.kwargs
        self.assertEqual(kwargs["cluster"], "attendee-bots")
        self.assertEqual(kwargs["taskDefinition"], "attendee-bot")
        self.assertEqual(kwargs["launchType"], "FARGATE")
        self.assertEqual(kwargs["startedBy"], "bot-42")
        self.assertEqual(kwargs["networkConfiguration"]["awsvpcConfiguration"]["subnets"], ["subnet-aaa", "subnet-bbb"])
        self.assertEqual(kwargs["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"], "DISABLED")

        override = kwargs["overrides"]["containerOverrides"][0]
        self.assertEqual(override["name"], "bot-proc")
        self.assertEqual(override["command"], ["python", "manage.py", "run_bot", "--botid", "42"])
        self.assertIn({"name": "IS_A_BOT_POD", "value": "true"}, override["environment"])

    def test_capacity_provider_replaces_launch_type(self):
        env = {**BASE_ENV, "BOT_TASK_CAPACITY_PROVIDER": "ec2-cp"}
        creator, mock_ecs = self._creator_with_mock_ecs(env)
        mock_ecs.run_task.return_value = {"tasks": [{"taskArn": "arn:task/abc"}], "failures": []}

        creator.create_bot_task(bot_id=1)

        kwargs = mock_ecs.run_task.call_args.kwargs
        self.assertNotIn("launchType", kwargs)
        self.assertEqual(kwargs["capacityProviderStrategy"][0]["capacityProvider"], "ec2-cp")

    def test_missing_config_returns_not_created(self):
        creator, mock_ecs = self._creator_with_mock_ecs({"BOT_TASK_DEFINITION": "td"})  # no cluster/subnets/sgs

        result = creator.create_bot_task(bot_id=1)

        self.assertFalse(result["created"])
        self.assertIn("BOT_ECS_CLUSTER", result["error"])
        mock_ecs.run_task.assert_not_called()

    def test_placement_failure_returns_not_created(self):
        creator, mock_ecs = self._creator_with_mock_ecs(BASE_ENV)
        # Non-transient failure, no tasks placed -> created False, no retry loop hang.
        mock_ecs.run_task.return_value = {"tasks": [], "failures": [{"reason": "AccessDeniedException"}]}

        result = creator.create_bot_task(bot_id=7)

        self.assertFalse(result["created"])

    @patch("bots.bot_ecs_task_creator.bot_ecs_task_creator.time.sleep")
    def test_transient_failure_retries_then_succeeds(self, mock_sleep):
        creator, mock_ecs = self._creator_with_mock_ecs(BASE_ENV)
        # First placement loses a capacity race (retryable), second succeeds.
        mock_ecs.run_task.side_effect = [
            {"tasks": [], "failures": [{"reason": "Capacity is unavailable at this time"}]},
            {"tasks": [{"taskArn": "arn:task/xyz"}], "failures": []},
        ]

        result = creator.create_bot_task(bot_id=5)

        self.assertTrue(result["created"])
        self.assertEqual(result["task_arn"], "arn:task/xyz")
        self.assertEqual(mock_ecs.run_task.call_count, 2)
        mock_sleep.assert_called()

    @patch("bots.bot_ecs_task_creator.bot_ecs_task_creator.time.sleep")
    def test_transient_client_error_retries_then_succeeds(self, mock_sleep):
        creator, mock_ecs = self._creator_with_mock_ecs(BASE_ENV)
        throttle = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "RunTask")
        mock_ecs.run_task.side_effect = [throttle, {"tasks": [{"taskArn": "arn:task/ok"}], "failures": []}]

        result = creator.create_bot_task(bot_id=6)

        self.assertTrue(result["created"])
        self.assertEqual(mock_ecs.run_task.call_count, 2)

    def test_non_transient_client_error_returns_not_created(self):
        creator, mock_ecs = self._creator_with_mock_ecs(BASE_ENV)
        mock_ecs.run_task.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "RunTask"
        )

        result = creator.create_bot_task(bot_id=8)

        self.assertFalse(result["created"])
        self.assertEqual(mock_ecs.run_task.call_count, 1)  # not retried

    def test_stop_bot_task(self):
        creator, mock_ecs = self._creator_with_mock_ecs(BASE_ENV)
        paginator = MagicMock()
        paginator.paginate.return_value = [{"taskArns": ["arn:task/abc"]}]
        mock_ecs.get_paginator.return_value = paginator

        result = creator.stop_bot_task(bot_id=42)

        self.assertTrue(result["stopped"])
        mock_ecs.stop_task.assert_called_once()
        self.assertEqual(mock_ecs.stop_task.call_args.kwargs["task"], "arn:task/abc")
