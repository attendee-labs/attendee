from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from bots.models import Credentials, Organization, Project

User = get_user_model()

WEBEX_CREDENTIALS = {"client_id": "C1234", "client_secret": "secret-9876", "refresh_token": "refresh-5432"}


class WebexServiceAppCredentialsTest(TestCase):
    def setUp(self):
        self.client = Client()
        organization = Organization.objects.create(name="Test Organization")
        self.user = User.objects.create_user(username="testuser", email="test@example.com", password="testpass123", organization=organization)
        self.project = Project.objects.create(name="Test Project", organization=organization)
        self.client.force_login(self.user)

    def post_credentials(self, data):
        return self.client.post(reverse("projects:create-credentials", args=[self.project.object_id]), {"credential_type": Credentials.CredentialTypes.WEBEX_SERVICE_APP, **data})

    def test_credentials_are_saved_encrypted(self):
        response = self.post_credentials(WEBEX_CREDENTIALS)

        self.assertEqual(response.status_code, 200)
        credential = Credentials.objects.get(project=self.project, credential_type=Credentials.CredentialTypes.WEBEX_SERVICE_APP)
        self.assertEqual(credential.get_credentials(), WEBEX_CREDENTIALS)
        self.assertNotIn(b"secret-9876", bytes(credential._encrypted_data))

    def test_secrets_are_masked_in_the_response(self):
        response = self.post_credentials(WEBEX_CREDENTIALS)

        self.assertContains(response, "C1234")
        self.assertNotContains(response, "secret-9876")
        self.assertNotContains(response, "refresh-5432")

    def test_every_field_is_required(self):
        for missing_field in WEBEX_CREDENTIALS:
            with self.subTest(missing_field=missing_field):
                response = self.post_credentials({key: value for key, value in WEBEX_CREDENTIALS.items() if key != missing_field})

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.content.decode(), "Missing required credentials data")

    def test_credentials_page_shows_webex_service_app_section(self):
        response = self.client.get(reverse("projects:project-credentials", args=[self.project.object_id]))

        self.assertContains(response, "Webex Service App Credentials")

    def test_credentials_can_be_deleted(self):
        self.post_credentials(WEBEX_CREDENTIALS)

        response = self.client.post(reverse("projects:delete-credentials", args=[self.project.object_id]), {"credential_type": Credentials.CredentialTypes.WEBEX_SERVICE_APP})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Credentials.objects.filter(project=self.project, credential_type=Credentials.CredentialTypes.WEBEX_SERVICE_APP).exists())
