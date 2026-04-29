from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.models import BotLogin, BotLoginGroup, BotLoginPlatform, Project


class TestBotLoginGroups(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_first_available_login_filters_by_group_name(self):
        group_a = BotLoginGroup.objects.create(project=self.project, platform=BotLoginPlatform.TEAMS, name="Group A")
        group_b = BotLoginGroup.objects.create(project=self.project, platform=BotLoginPlatform.TEAMS, name="Group B")

        BotLogin.objects.create(group=group_a, email="group-a@example.com")
        expected_login = BotLogin.objects.create(group=group_b, email="group-b@example.com")

        selected_login = BotLoginGroup.first_available_login(project=self.project, platform=BotLoginPlatform.TEAMS, group_name="Group B")
        self.assertEqual(selected_login, expected_login)

    def test_first_available_login_returns_login_from_named_group_even_if_inactive(self):
        group_without_active_logins = BotLoginGroup.objects.create(project=self.project, platform=BotLoginPlatform.TEAMS, name="Named Group")
        expected_login = BotLogin.objects.create(group=group_without_active_logins, email="inactive@example.com", is_active=False)

        other_group = BotLoginGroup.objects.create(project=self.project, platform=BotLoginPlatform.TEAMS, name="Other Group")
        BotLogin.objects.create(group=other_group, email="other@example.com")

        selected_login = BotLoginGroup.first_available_login(project=self.project, platform=BotLoginPlatform.TEAMS, group_name="Named Group")

        self.assertEqual(selected_login, expected_login)

    def test_first_available_login_uses_oldest_group_when_name_not_provided(self):
        first_group = BotLoginGroup.objects.create(project=self.project, platform=BotLoginPlatform.GOOGLE_MEET, name="First Group")
        expected_login = BotLogin.objects.create(group=first_group, email="inactive@example.com", is_active=False)

        second_group = BotLoginGroup.objects.create(project=self.project, platform=BotLoginPlatform.GOOGLE_MEET, name="Second Group")
        BotLogin.objects.create(group=second_group, email="active@example.com")

        selected_login = BotLoginGroup.first_available_login(project=self.project, platform=BotLoginPlatform.GOOGLE_MEET)

        self.assertEqual(selected_login, expected_login)

    def test_first_available_login_prefers_least_recently_used_login_within_group(self):
        group = BotLoginGroup.objects.create(project=self.project, platform=BotLoginPlatform.TEAMS, name="Shared Group")
        recently_used_login = BotLogin.objects.create(group=group, email="recent@example.com", last_used_at=timezone.now())
        never_used_login = BotLogin.objects.create(group=group, email="never-used@example.com", last_used_at=None)
        older_used_login = BotLogin.objects.create(group=group, email="older@example.com", last_used_at=timezone.now() - timedelta(days=1))

        selected_login = BotLoginGroup.first_available_login(project=self.project, platform=BotLoginPlatform.TEAMS)

        self.assertEqual(selected_login, never_used_login)
        self.assertNotEqual(selected_login, recently_used_login)
        self.assertNotEqual(selected_login, older_used_login)
