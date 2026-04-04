from django.test import TestCase


class NotificationsRoutingTests(TestCase):
    def test_categories_endpoint_exists(self):
        response = self.client.get("/api/v1/wasel/notification-categories/")
        self.assertIn(response.status_code, {200, 401})
