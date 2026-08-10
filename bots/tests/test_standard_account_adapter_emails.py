# accounts/tests/test_adapters.py

from unittest.mock import patch

from allauth.account.adapter import DefaultAccountAdapter
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings

from accounts.adapters import StandardAccountAdapter
from accounts.models import Organization


@override_settings(SITE_DOMAIN="attendee.dev")
class StandardAccountAdapterEmailUnitTests(SimpleTestCase):
    """
    Focused unit tests for the URL rewriting performed by send_mail().

    DefaultAccountAdapter.send_mail() is mocked here so these tests only verify
    the context that StandardAccountAdapter passes to allauth.
    """

    def setUp(self):
        self.adapter = StandardAccountAdapter()

    @patch.object(DefaultAccountAdapter, "send_mail")
    def test_rewrites_all_context_values_ending_in_url(self, mock_send_mail):
        context = {
            "activate_url": (
                "http://localhost:8000/accounts/confirm-email/abc123/"
            ),
            "password_reset_url": (
                "https://example.com/accounts/password/reset/key/123/"
            ),
            "username": "noah",
        }

        self.adapter.send_mail(
            "account/email/email_confirmation",
            "noah@example.com",
            context,
        )

        mock_send_mail.assert_called_once()

        template_prefix, email, passed_context = mock_send_mail.call_args.args

        self.assertEqual(
            template_prefix,
            "account/email/email_confirmation",
        )
        self.assertEqual(email, "noah@example.com")

        self.assertEqual(
            passed_context["activate_url"],
            "http://attendee.dev/accounts/confirm-email/abc123/",
        )
        self.assertEqual(
            passed_context["password_reset_url"],
            "https://attendee.dev/accounts/password/reset/key/123/",
        )

        # Unrelated context is unchanged.
        self.assertEqual(
            passed_context["username"],
            "noah",
        )

    @patch.object(DefaultAccountAdapter, "send_mail")
    def test_preserves_scheme_path_query_and_fragment(self, mock_send_mail):
        context = {
            "signup_url": (
                "https://localhost:8000/accounts/signup/"
                "?next=%2Fdashboard%2F#some-fragment"
            ),
        }

        self.adapter.send_mail(
            "account/email/unknown_account",
            "noah@example.com",
            context,
        )

        passed_context = mock_send_mail.call_args.args[2]

        self.assertEqual(
            passed_context["signup_url"],
            (
                "https://attendee.dev/accounts/signup/"
                "?next=%2Fdashboard%2F#some-fragment"
            ),
        )

    @patch.object(DefaultAccountAdapter, "send_mail")
    def test_only_rewrites_keys_ending_in_url(self, mock_send_mail):
        context = {
            "signup_url": "http://localhost:8000/accounts/signup/",
            "website": "http://localhost:8000/",
            "url_description": "http://localhost:8000/foo/",
        }

        self.adapter.send_mail(
            "account/email/unknown_account",
            "noah@example.com",
            context,
        )

        passed_context = mock_send_mail.call_args.args[2]

        self.assertEqual(
            passed_context["signup_url"],
            "http://attendee.dev/accounts/signup/",
        )

        # These contain URLs but their keys do not end in "_url".
        self.assertEqual(
            passed_context["website"],
            "http://localhost:8000/",
        )
        self.assertEqual(
            passed_context["url_description"],
            "http://localhost:8000/foo/",
        )

    @patch.object(DefaultAccountAdapter, "send_mail")
    def test_does_not_rewrite_non_string_url_values(self, mock_send_mail):
        context = {
            "something_url": None,
            "another_url": 123,
        }

        self.adapter.send_mail(
            "account/email/test",
            "noah@example.com",
            context,
        )

        passed_context = mock_send_mail.call_args.args[2]

        self.assertIsNone(passed_context["something_url"])
        self.assertEqual(passed_context["another_url"], 123)

    @patch.object(DefaultAccountAdapter, "send_mail")
    def test_does_not_mutate_original_context(self, mock_send_mail):
        context = {
            "activate_url": (
                "http://localhost:8000/accounts/confirm-email/abc123/"
            ),
        }

        self.adapter.send_mail(
            "account/email/email_confirmation",
            "noah@example.com",
            context,
        )

        # The caller's context should not be changed.
        self.assertEqual(
            context["activate_url"],
            "http://localhost:8000/accounts/confirm-email/abc123/",
        )

        # But the context passed to allauth should be changed.
        passed_context = mock_send_mail.call_args.args[2]

        self.assertEqual(
            passed_context["activate_url"],
            "http://attendee.dev/accounts/confirm-email/abc123/",
        )


@override_settings(
    SITE_DOMAIN="attendee.dev",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="test@example.com",
)
class StandardAccountAdapterRenderedEmailTests(TestCase):
    """
    Integration test using the real allauth send_mail() implementation and
    real email templates.

    This makes sure our override didn't break allauth email rendering/sending
    and that the rendered email actually contains the rewritten URL.
    """

    def setUp(self):
        self.adapter = StandardAccountAdapter()

        self.organization = Organization.objects.create(name="Test Organization")

        User = get_user_model()
        self.user = User.objects.create_user(
            username="noah",
            email="noah@example.com",
            password="password",
            organization=self.organization,
        )

    def test_email_is_rendered_and_sent_with_site_domain(self):
        original_url = (
            "http://localhost:8000/accounts/confirm-email/abc123/"
            "?next=%2Fdashboard%2F#confirmed"
        )

        expected_url = (
            "http://attendee.dev/accounts/confirm-email/abc123/"
            "?next=%2Fdashboard%2F#confirmed"
        )

        self.adapter.send_mail(
            "account/email/email_confirmation",
            self.user.email,
            {
                "user": self.user,
                "activate_url": original_url,
            },
        )

        # A real email was successfully rendered and sent.
        self.assertEqual(len(mail.outbox), 1)

        message = mail.outbox[0]

        self.assertEqual(
            message.to,
            [self.user.email],
        )
        self.assertTrue(message.subject)
        self.assertTrue(message.body)

        # Check all rendered representations of the email, including the
        # plain-text body and any HTML alternative.
        rendered_content = [message.body]

        for alternative in message.alternatives:
            rendered_content.append(alternative.content)

        rendered_email = "\n".join(rendered_content)

        # The final rendered email contains the rewritten URL.
        self.assertIn(
            expected_url,
            rendered_email,
        )

        # Nothing leaked through with the original hostname.
        self.assertNotIn(
            "localhost:8000",
            rendered_email,
        )