"""
Tests for the delete credentials functionality.
"""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from bots.models import Credentials, Organization, Project

User = get_user_model()


class DeleteCredentialsViewTest(TestCase):
    """Test the DeleteCredentialsView functionality."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()

        # Create test organization
        self.organization = Organization.objects.create(name="Test Organization")

        # Create test user
        self.user = User.objects.create_user(username="testuser", email="test@example.com", password="testpass123", organization=self.organization)

        # Create test project
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Create test credentials for different providers
        self.deepgram_cred = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM)

        self.openai_cred = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.OPENAI)

        self.zoom_cred = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH)

    def test_delete_credentials_success(self):
        """Test successful deletion of credentials."""
        self.client.force_login(self.user)

        # Verify credential exists
        self.assertTrue(Credentials.objects.filter(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM).exists())

        # Delete the credential
        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"credential_type": Credentials.CredentialTypes.DEEPGRAM, "csrfmiddlewaretoken": "test-token"})

        # Check response
        self.assertEqual(response.status_code, 200)

        # Verify credential is deleted
        self.assertFalse(Credentials.objects.filter(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM).exists())

    def test_delete_credentials_invalid_credential_type(self):
        """Test deletion with invalid credential type."""
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("projects:delete-credentials", args=[self.project.object_id]),
            {
                "credential_type": 999,  # Invalid credential type
                "csrfmiddlewaretoken": "test-token",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Invalid credential type")

    def test_delete_credentials_nonexistent_credential(self):
        """Test deletion of non-existent credential."""
        self.client.force_login(self.user)

        # Try to delete a credential that doesn't exist
        response = self.client.post(
            reverse("projects:delete-credentials", args=[self.project.object_id]),
            {
                "credential_type": Credentials.CredentialTypes.GLADIA,  # Not created in setUp
                "csrfmiddlewaretoken": "test-token",
            },
        )

        # Should still return 200 (successful deletion of nothing)
        self.assertEqual(response.status_code, 200)

    def test_delete_credentials_unauthorized_user(self):
        """Test deletion by unauthorized user."""
        # Create another user from different organization
        other_org = Organization.objects.create(name="Other Organization")
        other_user = User.objects.create_user(username="otheruser", email="other@example.com", password="testpass123", organization=other_org)

        self.client.force_login(other_user)

        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"credential_type": Credentials.CredentialTypes.DEEPGRAM, "csrfmiddlewaretoken": "test-token"})

        # Should be forbidden (403), redirect to login (302), or not found (404)
        self.assertIn(response.status_code, [403, 302, 404])

    def test_delete_credentials_missing_credential_type(self):
        """Test deletion without credential_type parameter."""
        self.client.force_login(self.user)

        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"csrfmiddlewaretoken": "test-token"})

        self.assertEqual(response.status_code, 400)

    def test_delete_credentials_invalid_credential_type_format(self):
        """Test deletion with non-integer credential_type."""
        self.client.force_login(self.user)

        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"credential_type": "not-a-number", "csrfmiddlewaretoken": "test-token"})

        self.assertEqual(response.status_code, 400)

    def test_delete_credentials_renders_correct_template(self):
        """Test that deletion renders the correct template for each credential type."""
        self.client.force_login(self.user)

        # Test Deepgram
        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"credential_type": Credentials.CredentialTypes.DEEPGRAM, "csrfmiddlewaretoken": "test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("deepgram-credentials-container", response.content.decode())

        # Test OpenAI
        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"credential_type": Credentials.CredentialTypes.OPENAI, "csrfmiddlewaretoken": "test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("openai-credentials-container", response.content.decode())

        # Test Zoom
        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"credential_type": Credentials.CredentialTypes.ZOOM_OAUTH, "csrfmiddlewaretoken": "test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("zoom-credentials-container", response.content.decode())

    def test_delete_credentials_unsupported_credential_type(self):
        """Test deletion with credential type that has no template."""
        self.client.force_login(self.user)

        # Create a credential with a type that doesn't have a template
        # (This would be a future credential type not yet supported in the view)
        response = self.client.post(
            reverse("projects:delete-credentials", args=[self.project.object_id]),
            {
                "credential_type": 999,  # Invalid/unsupported type
                "csrfmiddlewaretoken": "test-token",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Invalid credential type")

    def test_delete_credentials_preserves_other_credentials(self):
        """Test that deleting one credential doesn't affect others."""
        self.client.force_login(self.user)

        # Verify both credentials exist
        self.assertTrue(Credentials.objects.filter(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM).exists())
        self.assertTrue(Credentials.objects.filter(project=self.project, credential_type=Credentials.CredentialTypes.OPENAI).exists())

        # Delete only Deepgram credential
        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"credential_type": Credentials.CredentialTypes.DEEPGRAM, "csrfmiddlewaretoken": "test-token"})

        self.assertEqual(response.status_code, 200)

        # Verify Deepgram is deleted but OpenAI remains
        self.assertFalse(Credentials.objects.filter(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM).exists())
        self.assertTrue(Credentials.objects.filter(project=self.project, credential_type=Credentials.CredentialTypes.OPENAI).exists())
