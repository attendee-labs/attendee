import json
from unittest.mock import patch

from django.test import Client, TestCase

from accounts.models import Organization
from bots.models import ApiKey, Project, RecorderUpload, RecorderUploadStates

STORAGE = "bots.recorder_upload_storage"


class RecorderSessionsApiTestCase(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Org A")
        self.project = Project.objects.create(name="Project A", organization=self.organization)
        self.api_key, self.api_key_plain = ApiKey.create(project=self.project, name="Key A")

        # A second project/key to assert cross-project isolation.
        self.other_org = Organization.objects.create(name="Org B")
        self.other_project = Project.objects.create(name="Project B", organization=self.other_org)
        self.other_api_key, self.other_api_key_plain = ApiKey.create(project=self.other_project, name="Key B")

        self.client = Client()

        # Patch the S3 storage layer for all endpoints.
        patches = {
            f"{STORAGE}.recorder_uploads_supported": patch(f"{STORAGE}.recorder_uploads_supported", return_value=True),
            f"{STORAGE}.initiate_multipart_upload": patch(f"{STORAGE}.initiate_multipart_upload", return_value="upload-xyz"),
            f"{STORAGE}.complete_multipart_upload": patch(f"{STORAGE}.complete_multipart_upload", return_value=None),
            f"{STORAGE}.abort_multipart_upload": patch(f"{STORAGE}.abort_multipart_upload", return_value=None),
            f"{STORAGE}.object_size": patch(f"{STORAGE}.object_size", return_value=4096),
            f"{STORAGE}.list_uploaded_parts": patch(f"{STORAGE}.list_uploaded_parts", return_value=[{"part_number": 1, "etag": "e1", "size": 10}]),
            f"{STORAGE}.generate_part_upload_urls": patch(f"{STORAGE}.generate_part_upload_urls", side_effect=lambda s3_key, upload_id, part_numbers: [{"part_number": n, "url": f"https://s3.example/{n}"} for n in part_numbers]),
        }
        for p in patches.values():
            p.start()
            self.addCleanup(p.stop)

    def _post(self, url, data=None, key=None):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json", HTTP_AUTHORIZATION=f"Token {key or self.api_key_plain}")

    def _get(self, url, key=None):
        return self.client.get(url, HTTP_AUTHORIZATION=f"Token {key or self.api_key_plain}")

    def _create_session(self, num_parts=2, key=None):
        return self._post("/api/v1/recorder_sessions", {"content_type": "video/mp4", "num_parts": num_parts}, key=key)

    # --- create -------------------------------------------------------------------

    def test_create_returns_session_and_part_urls(self):
        response = self._create_session(num_parts=3)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["id"].startswith("drec_"))
        self.assertEqual(body["state"], "created")
        self.assertEqual(len(body["upload"]["part_urls"]), 3)
        self.assertEqual(body["upload"]["upload_id"], "upload-xyz")

    def test_create_requires_authentication(self):
        response = self.client.post("/api/v1/recorder_sessions", data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 401)

    # --- status / resume ----------------------------------------------------------

    def test_status_returns_received_parts(self):
        session_id = self._create_session().json()["id"]
        response = self._get(f"/api/v1/recorder_sessions/{session_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["received_parts"], [{"part_number": 1, "etag": "e1", "size": 10}])

    def test_status_cross_project_is_404(self):
        session_id = self._create_session().json()["id"]
        response = self._get(f"/api/v1/recorder_sessions/{session_id}", key=self.other_api_key_plain)
        self.assertEqual(response.status_code, 404)

    # --- more part urls -----------------------------------------------------------

    def test_parts_endpoint_issues_more_urls(self):
        session_id = self._create_session().json()["id"]
        response = self._post(f"/api/v1/recorder_sessions/{session_id}/parts", {"part_numbers": [4, 5]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([u["part_number"] for u in response.json()["upload"]["part_urls"]], [4, 5])

    # --- complete -----------------------------------------------------------------

    def test_complete_finalizes_session(self):
        session_id = self._create_session().json()["id"]
        response = self._post(f"/api/v1/recorder_sessions/{session_id}/complete", {"parts": [{"part_number": 1, "etag": "e1"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "complete")
        self.assertEqual(response.json()["recording_state"], "complete")

    def test_complete_is_idempotent(self):
        session_id = self._create_session().json()["id"]
        self._post(f"/api/v1/recorder_sessions/{session_id}/complete", {"parts": [{"part_number": 1, "etag": "e1"}]})
        response = self._post(f"/api/v1/recorder_sessions/{session_id}/complete", {"parts": [{"part_number": 1, "etag": "e1"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "complete")

    # --- abort --------------------------------------------------------------------

    def test_abort_session(self):
        session_id = self._create_session().json()["id"]
        response = self._post(f"/api/v1/recorder_sessions/{session_id}/abort")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "expired")

    def test_abort_completed_session_is_400(self):
        session_id = self._create_session().json()["id"]
        self._post(f"/api/v1/recorder_sessions/{session_id}/complete", {"parts": [{"part_number": 1, "etag": "e1"}]})
        response = self._post(f"/api/v1/recorder_sessions/{session_id}/abort")
        self.assertEqual(response.status_code, 400)

    # --- account ------------------------------------------------------------------

    def test_account_endpoint(self):
        response = self._get("/api/v1/account")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["project"]["object_id"], self.project.object_id)
        self.assertEqual(body["project"]["name"], "Project A")
        self.assertEqual(body["organization"]["name"], "Org A")
        self.assertEqual(body["api_key"]["name"], "Key A")

    def test_account_requires_authentication(self):
        response = self.client.get("/api/v1/account")
        self.assertEqual(response.status_code, 401)


class RecorderSessionReaperTestCase(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Project", organization=self.organization)

    @patch(f"{STORAGE}.recorder_uploads_supported", return_value=True)
    @patch(f"{STORAGE}.initiate_multipart_upload", return_value="upload-1")
    @patch(f"{STORAGE}.abort_multipart_upload", return_value=None)
    def test_reaper_expires_abandoned_sessions(self, _abort, _init, _supported):
        from django.core.management import call_command
        from django.utils import timezone

        from bots.recorder_sessions_api_utils import create_recorder_session

        recorder_upload, _ = create_recorder_session({}, project=self.project)
        # Force the session to look abandoned.
        RecorderUpload.objects.filter(id=recorder_upload.id).update(last_activity_at=timezone.now() - timezone.timedelta(hours=5))

        call_command("clean_up_abandoned_recorder_sessions")

        recorder_upload.refresh_from_db()
        self.assertEqual(recorder_upload.state, RecorderUploadStates.EXPIRED)
        _abort.assert_called_once()
