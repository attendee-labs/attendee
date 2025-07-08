import re

from allauth.account.models import EmailAddress
from django.core import mail
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization, User
from bots.models import Project


class InviteUserIntegrationTest(TransactionTestCase):
    """Integration test for the complete user invitation flow"""

    def setUp(self):
        """Set up test environment"""
        # Create test organization and user
        self.organization = Organization.objects.create(name="Test Organization")
        self.inviting_user = User.objects.create_user(username="inviter", email="inviter@example.com", password="testpassword123")
        self.inviting_user.organization = self.organization
        self.inviting_user.save()

        # Create test project
        self.project = Project.objects.create(
            name="Test Project",
            organization=self.organization,
        )

        # Test data
        self.invited_email = "invited@example.com"

        # Create test client
        self.client = Client()

        # Clear any existing emails
        mail.outbox = []

    def test_invite_user_happy_path(self):
        """Test the complete happy path of inviting a user"""

        # Step 1: Send POST request to invite endpoint as authenticated user
        self.client.force_login(self.inviting_user)

        invite_url = reverse("projects:invite-user", kwargs={"object_id": self.project.object_id})
        response = self.client.post(
            invite_url,
            {
                "email": self.invited_email,
            },
        )

        # Verify invite was successful
        self.assertEqual(response.status_code, 200)
        self.assertIn("Invitation sent successfully", response.content.decode())

        # Verify user was created
        invited_user = User.objects.get(email=self.invited_email)
        self.assertEqual(invited_user.invited_by, self.inviting_user)
        self.assertEqual(invited_user.organization, self.organization)
        self.assertTrue(invited_user.is_active)

        # Verify email was sent
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, [self.invited_email])
        self.assertIn("invited you to join", email.body)

        # Step 2: Extract confirmation URL from email and visit it as unauthenticated user
        # The email should contain a confirmation URL
        email_body = email.body
        url_pattern = r"http://testserver(/accounts/confirm-email/[^/\s]+/)"
        match = re.search(url_pattern, email_body)
        self.assertIsNotNone(match, "Email confirmation URL not found in email body")

        confirmation_url = match.group(1)

        # Log out the inviting user to simulate unauthenticated session
        self.client.logout()

        invited_user.refresh_from_db()
        self.assertIsNone(invited_user.last_login)

        # Visit the confirmation URL
        response = self.client.get(confirmation_url)

        # Should redirect to password set page for invited users
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("account_set_password"))

        # Verify user's last_sign_in_at is set
        invited_user.refresh_from_db()
        self.assertIsNotNone(invited_user.last_login)

        # Step 3: Set password and verify redirect to home page
        password_set_url = reverse("account_set_password")
        response = self.client.post(
            password_set_url,
            {
                "password1": "newpassword123",
                "password2": "newpassword123",
                "next": "/",
            },
        )

        # Should redirect to '/' which in turn redirects to the project dashboard
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, "/", target_status_code=302)

        # Verify the second redirect goes to the project dashboard
        response = self.client.get("/")
        expected_dashboard_url = reverse("projects:project-dashboard", kwargs={"object_id": self.project.object_id})
        self.assertRedirects(response, expected_dashboard_url)

        # Verify user can now log in with the new password
        self.client.logout()
        login_success = self.client.login(username=invited_user.username, password="newpassword123")
        self.assertTrue(login_success)

        # Verify user's email is confirmed
        email_address = EmailAddress.objects.get(user=invited_user, email=self.invited_email)
        self.assertTrue(email_address.verified)

    def test_invite_user_duplicate_email(self):
        """Test that inviting a user with an existing email fails"""
        # Create a user with the email we want to invite
        existing_user = User.objects.create_user(username="existing", email=self.invited_email, password="password123")
        existing_user.organization = self.organization
        existing_user.save()

        # Try to invite the same email
        self.client.force_login(self.inviting_user)

        invite_url = reverse("projects:invite-user", kwargs={"object_id": self.project.object_id})
        response = self.client.post(
            invite_url,
            {
                "email": self.invited_email,
            },
        )

        # Should fail with error
        self.assertEqual(response.status_code, 400)
        self.assertIn("A user with this email already exists", response.content.decode())

        # No email should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_invite_user_missing_email(self):
        """Test that inviting without an email fails"""
        self.client.force_login(self.inviting_user)

        invite_url = reverse("projects:invite-user", kwargs={"object_id": self.project.object_id})
        response = self.client.post(
            invite_url,
            {
                # No email provided
            },
        )

        # Should fail with error
        self.assertEqual(response.status_code, 400)
        self.assertIn("Email is required", response.content.decode())

        # No email should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_invite_user_unauthorized(self):
        """Test that unauthenticated users cannot invite"""
        # Don't log in
        invite_url = reverse("projects:invite-user", kwargs={"object_id": self.project.object_id})
        response = self.client.post(
            invite_url,
            {
                "email": self.invited_email,
            },
        )

        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, f"/accounts/login/?next={invite_url}")

        # No email should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_invite_user_wrong_organization(self):
        """Test that users cannot invite to projects they don't have access to"""
        # Create another organization and project
        other_org = Organization.objects.create(name="Other Organization")
        other_project = Project.objects.create(
            name="Other Project",
            organization=other_org,
        )

        # Try to invite to the other project
        self.client.force_login(self.inviting_user)

        invite_url = reverse("projects:invite-user", kwargs={"object_id": other_project.object_id})
        response = self.client.post(
            invite_url,
            {
                "email": self.invited_email,
            },
        )

        # Should return 404 because get_object_or_404 filters by organization
        self.assertEqual(response.status_code, 404)

        # No email should be sent
        self.assertEqual(len(mail.outbox), 0)
