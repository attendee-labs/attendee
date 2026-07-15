import re
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import requests
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin, SocialToken
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from django.contrib.sites.models import Site
from django.core import mail
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from accounts.models import User
from bots.models import Project


class UserSignupIntegrationTest(TransactionTestCase):
    """Integration test for the complete user signup flow"""

    def setUp(self):
        """Set up test environment"""
        # Test data. Use a unique address per test so allauth's per-email
        # confirm_email rate limit (1/180s/key) doesn't leak across tests via
        # the process-global cache.
        self.signup_email = f"newuser-{uuid.uuid4().hex}@example.com"
        self.password = "testpassword123"

        # Create test client
        self.client = Client()

        # Clear any existing emails
        mail.outbox = []

    def test_user_signup_happy_path(self):
        """Test the complete happy path of user signup"""

        # Step 1: Submit signup form
        signup_url = reverse("account_signup")
        response = self.client.post(
            signup_url,
            {
                "email": self.signup_email,
                "password1": self.password,
                "password2": self.password,
            },
        )

        # Should redirect to verification sent page
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("account_email_verification_sent"))

        # Verify user was created but not yet active/verified
        user = User.objects.get(email=self.signup_email)
        self.assertIsNotNone(user)
        self.assertIsNone(user.invited_by)  # Not an invited user
        self.assertIsNotNone(user.organization)  # Organization should be created
        self.assertTrue(user.is_active)  # User should be active

        # Verify organization and project were created
        organization = user.organization
        self.assertIn(self.signup_email, organization.name)

        # Verify default project was created
        project = Project.objects.get(organization=organization)
        self.assertIn(self.signup_email, project.name)

        # Verify verification email was sent
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, [self.signup_email])
        self.assertIn("confirm", email.body.lower())
        self.assertNotIn("invited you to join", email.body)  # Should not be invitation email

        # Step 2: Extract confirmation URL from email and visit it
        email_body = email.body
        url_pattern = r"http://testserver(/accounts/confirm-email/[^/\s]+/)"
        match = re.search(url_pattern, email_body)
        self.assertIsNotNone(match, "Email confirmation URL not found in email body")

        confirmation_url = match.group(1)

        # Visit the confirmation URL
        response = self.client.get(confirmation_url)

        # Should redirect to login page since this is a normal signup (not invited)
        self.assertEqual(response.status_code, 302)
        # The StandardAccountAdapter should redirect to parent's get_email_verification_redirect_url
        # which typically redirects to settings.LOGIN_REDIRECT_URL or login page

        # Verify user's last_login is set (since ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION is True)
        user.refresh_from_db()
        self.assertIsNotNone(user.last_login)

        # Verify user's email is confirmed
        email_address = EmailAddress.objects.get(user=user, email=self.signup_email)
        self.assertTrue(email_address.verified)

        # Step 3: Test that user can log in and is redirected to dashboard
        login_url = reverse("account_login")
        response = self.client.post(
            login_url,
            {
                "login": self.signup_email,
                "password": self.password,
            },
        )

        # Should redirect to home page
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, "/", target_status_code=302)

        # Verify the redirect to home goes to the project dashboard
        response = self.client.get("/")
        expected_dashboard_url = reverse("projects:project-dashboard", kwargs={"object_id": project.object_id})
        self.assertRedirects(response, expected_dashboard_url)

        # Verify user can access the dashboard
        response = self.client.get(expected_dashboard_url)
        self.assertEqual(response.status_code, 200)

    def test_user_signup_password_mismatch(self):
        """Test that signup fails when passwords don't match"""
        signup_url = reverse("account_signup")
        response = self.client.post(
            signup_url,
            {
                "email": self.signup_email,
                "password1": self.password,
                "password2": "differentpassword123",
            },
        )

        # Should return form with error
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "password")

        # No user should be created
        self.assertFalse(User.objects.filter(email=self.signup_email).exists())

        # No email should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_user_signup_missing_fields(self):
        """Test that signup fails when required fields are missing"""
        signup_url = reverse("account_signup")

        # Test missing email
        response = self.client.post(
            signup_url,
            {
                "password1": self.password,
                "password2": self.password,
            },
        )

        # Should return form with error
        self.assertEqual(response.status_code, 200)

        # No user should be created
        self.assertFalse(User.objects.filter(email=self.signup_email).exists())

        # No email should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_user_signup_weak_password(self):
        """Test that signup fails with weak password"""
        signup_url = reverse("account_signup")
        weak_password = "123"  # Too short and common

        response = self.client.post(
            signup_url,
            {
                "email": self.signup_email,
                "password1": weak_password,
                "password2": weak_password,
            },
        )

        # Should return form with error
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "password")

        # No user should be created
        self.assertFalse(User.objects.filter(email=self.signup_email).exists())

        # No email should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_user_signup_invalid_email(self):
        """Test that signup fails with invalid email format"""
        signup_url = reverse("account_signup")

        response = self.client.post(
            signup_url,
            {
                "email": "invalid-email",
                "password1": self.password,
                "password2": self.password,
            },
        )

        # Should return form with error
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "email")

        # No user should be created
        self.assertFalse(User.objects.filter(email="invalid-email").exists())

        # No email should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_signup_when_disabled(self):
        """Test that signup is disabled when DISABLE_SIGNUP is set"""
        with self.settings(ACCOUNT_ADAPTER="accounts.adapters.NoNewUsersAccountAdapter"):
            signup_url = reverse("account_signup")
            response = self.client.get(signup_url)

            # Should return 200 and show page with signup closed message
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "We are sorry, but the sign up is currently closed")
            self.assertContains(response, "Sign Up Closed")


@override_settings(
    MAILGUN_VALIDATION_API_KEY="test-mailgun-key",
    BYPASS_MAILGUN_VALIDATION_SUBSTRING=None,
)
class SignupMailgunValidationTest(TransactionTestCase):
    """Tests for the Mailgun email validation performed in StandardAccountAdapter.clean_email"""

    def setUp(self):
        self.signup_email = f"newuser-{uuid.uuid4().hex}@example.com"
        self.password = "testpassword123"
        self.client = Client()
        mail.outbox = []

    def _post_signup(self):
        return self.client.post(
            reverse("account_signup"),
            {
                "email": self.signup_email,
                "password1": self.password,
                "password2": self.password,
            },
        )

    @staticmethod
    def _mailgun_response(payload):
        """Build a fake requests.Response-like object for the Mailgun call."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = payload
        return mock_response

    @patch("accounts.adapters.requests.post")
    def test_signup_rejected_for_disposable_email(self, mock_post):
        """A disposable address should block signup with a validation error."""
        mock_post.return_value = self._mailgun_response({"is_disposable_address": True, "result": "deliverable"})

        response = self._post_signup()

        # Form should be re-rendered with the validation error
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please use a permanent email address.")

        # Mailgun should have been consulted and no user created
        mock_post.assert_called_once()
        self.assertFalse(User.objects.filter(email=self.signup_email).exists())
        self.assertEqual(len(mail.outbox), 0)

    @patch("accounts.adapters.requests.post")
    def test_signup_rejected_for_undeliverable_email(self, mock_post):
        """An undeliverable result should block signup with a validation error."""
        mock_post.return_value = self._mailgun_response({"is_disposable_address": False, "result": "undeliverable"})

        response = self._post_signup()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This email address does not appear to be valid.")
        self.assertFalse(User.objects.filter(email=self.signup_email).exists())
        self.assertEqual(len(mail.outbox), 0)

    @patch("accounts.adapters.requests.post")
    def test_signup_rejected_for_do_not_send_email(self, mock_post):
        """A do_not_send result should block signup with a validation error."""
        mock_post.return_value = self._mailgun_response({"is_disposable_address": False, "result": "do_not_send"})

        response = self._post_signup()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This email address does not appear to be valid.")
        self.assertFalse(User.objects.filter(email=self.signup_email).exists())
        self.assertEqual(len(mail.outbox), 0)

    @patch("accounts.adapters.requests.post")
    def test_signup_allowed_for_deliverable_email(self, mock_post):
        """A deliverable, non-disposable address should be allowed through."""
        mock_post.return_value = self._mailgun_response({"is_disposable_address": False, "result": "deliverable"})

        response = self._post_signup()

        self.assertRedirects(response, reverse("account_email_verification_sent"))
        mock_post.assert_called_once()
        self.assertTrue(User.objects.filter(email=self.signup_email).exists())
        self.assertEqual(len(mail.outbox), 1)

    @patch("accounts.adapters.requests.post")
    def test_signup_allowed_when_mailgun_request_fails(self, mock_post):
        """If the Mailgun request raises, validation should fail open and allow signup."""
        mock_post.side_effect = requests.RequestException("boom")

        response = self._post_signup()

        self.assertRedirects(response, reverse("account_email_verification_sent"))
        mock_post.assert_called_once()
        self.assertTrue(User.objects.filter(email=self.signup_email).exists())
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(BYPASS_MAILGUN_VALIDATION_SUBSTRING="example.com")
    @patch("accounts.adapters.requests.post")
    def test_signup_bypasses_mailgun_for_matching_substring(self, mock_post):
        """Emails matching the bypass substring should skip the Mailgun call entirely."""
        response = self._post_signup()

        self.assertRedirects(response, reverse("account_email_verification_sent"))
        mock_post.assert_not_called()
        self.assertTrue(User.objects.filter(email=self.signup_email).exists())
        self.assertEqual(len(mail.outbox), 1)


@override_settings(
    SOCIALACCOUNT_LOGIN_ON_GET=True,  # auto-log users in on GET callback
    LOGIN_REDIRECT_URL="/",  # where allauth should send us after login
)
class GoogleSocialLoginHappyPathTest(TransactionTestCase):
    """End-to-end happy-path test for Google OAuth2 login"""

    def setUp(self):
        self.client = Client()

        # Minimal SocialApp so allauth recognises the provider for this site
        self.social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="dummy-client-id",
            secret="dummy-secret",
        )
        self.social_app.sites.add(Site.objects.get_current())

    @patch.object(
        OAuth2Client,
        "get_access_token",
        return_value={"access_token": "dummy-token", "expires_in": 3600, "token_type": "Bearer"},
    )
    @patch("allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter.complete_login")
    def test_google_social_login_success(self, mocked_complete_login, mocked_get_access_token):
        """
        Simulate a full OAuth2 login flow and assert:
        * user & SocialAccount are created
        * user ends up authenticated
        * redirected to LOGIN_REDIRECT_URL
        """

        # ----------  Arrange mock SocialLogin  ----------
        email = "socialuser@example.com"
        uid = f"google-oauth2-{uuid.uuid4()}"

        user = User(email=email)

        social_account = SocialAccount(
            provider="google",
            uid=uid,
            extra_data={"email": email, "name": "Social User"},
        )

        token = SocialToken(token="dummy-token", app=self.social_app, account=social_account)

        sociallogin = SocialLogin(user=user, account=social_account, token=token)

        # Tell Allauth the e-mail is verified and primary
        sociallogin.email_addresses.append(EmailAddress(email=email, verified=True, primary=True))

        mocked_complete_login.return_value = sociallogin

        # ----------  Step 1: user clicks “Sign in with Google”  ----------
        start_url = reverse("google_login")  # /accounts/google/login/
        resp = self.client.get(start_url)

        # allauth should redirect us to Google’s auth endpoint
        self.assertEqual(resp.status_code, 302)
        self.assertIn("accounts.google.com", resp["Location"])

        # Allauth has built the Google URL; grab the “state” it stored
        parsed = urlparse(resp["Location"])
        state = parse_qs(parsed.query)["state"][0]

        # ----------  Step 2: Google redirects back with code+state  ----------
        callback_url = reverse("google_callback")  # /accounts/google/login/callback/
        resp = self.client.get(callback_url, {"state": state, "code": "dummy"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/")  # LOGIN_REDIRECT_URL

        # ----------  Assertions  ----------
        # user exists & is authenticated in session
        db_user = User.objects.get(email=email)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(str(db_user.pk), self.client.session["_auth_user_id"])

        # SocialAccount linked correctly
        self.assertTrue(SocialAccount.objects.filter(user=db_user, provider="google", uid=uid).exists())

        # Accessing home should work
        home_resp = self.client.get("/")
        self.assertEqual(home_resp.status_code, 302)
        self.assertRedirects(home_resp, reverse("projects:project-dashboard", kwargs={"object_id": db_user.organization.projects.first().object_id}))
