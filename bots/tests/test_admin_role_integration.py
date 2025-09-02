from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Project, ProjectAccess


class AdminRoleIntegrationTest(TransactionTestCase):
    """Integration tests for admin-restricted endpoints"""

    def setUp(self):
        """Set up test environment"""
        # Create test organization
        self.organization = Organization.objects.create(name="Test Organization", centicredits=10000)

        # Create admin user
        self.admin_user = User.objects.create_user(username="admin", email="admin@example.com", password="testpassword123", role=UserRole.ADMIN)
        self.admin_user.organization = self.organization
        self.admin_user.save()

        # Create regular user
        self.regular_user = User.objects.create_user(username="regular", email="regular@example.com", password="testpassword123", role=UserRole.REGULAR_USER)
        self.regular_user.organization = self.organization
        self.regular_user.save()

        # Create another regular user for user management tests
        self.target_user = User.objects.create_user(username="target", email="target@example.com", password="testpassword123", role=UserRole.REGULAR_USER)
        self.target_user.organization = self.organization
        self.target_user.save()

        # Create test project
        self.project = Project.objects.create(
            name="Test Project",
            organization=self.organization,
        )

        # Give regular users access to the project for testing
        ProjectAccess.objects.create(project=self.project, user=self.regular_user)
        ProjectAccess.objects.create(project=self.project, user=self.target_user)

        # Create test client
        self.client = Client()

    def test_create_project_admin_access(self):
        """Test that admin users can create projects"""
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse("bots:create-project"), data={"name": "New Test Project"})

        # Should redirect to new project dashboard (success)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Project.objects.filter(name="New Test Project").exists())

    def test_create_project_non_admin_denied(self):
        """Test that non-admin users cannot create projects"""
        self.client.force_login(self.regular_user)

        response = self.client.post(reverse("bots:create-project"), data={"name": "New Test Project"})

        # Should return 403 Forbidden
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Project.objects.filter(name="New Test Project").exists())

    def test_project_project_view_admin_access(self):
        """Test that admin users can access project settings"""
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("bots:project-project", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Project")

    def test_project_project_view_non_admin_denied(self):
        """Test that non-admin users cannot access project settings"""
        self.client.force_login(self.regular_user)

        response = self.client.get(reverse("bots:project-project", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 403)

    def test_edit_project_admin_access(self):
        """Test that admin users can edit projects"""
        self.client.force_login(self.admin_user)

        response = self.client.put(reverse("bots:project-edit", kwargs={"object_id": self.project.object_id}), data="name=Updated Project Name", content_type="application/x-www-form-urlencoded")

        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Updated Project Name")

    def test_edit_project_non_admin_denied(self):
        """Test that non-admin users cannot edit projects"""
        self.client.force_login(self.regular_user)
        original_name = self.project.name

        response = self.client.put(reverse("bots:project-edit", kwargs={"object_id": self.project.object_id}), data="name=Updated Project Name", content_type="application/x-www-form-urlencoded")

        self.assertEqual(response.status_code, 403)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, original_name)

    def test_project_billing_admin_access(self):
        """Test that admin users can access billing"""
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("bots:project-billing", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 200)

    def test_project_billing_non_admin_denied(self):
        """Test that non-admin users cannot access billing"""
        self.client.force_login(self.regular_user)

        response = self.client.get(reverse("bots:project-billing", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 403)

    def test_project_team_view_admin_access(self):
        """Test that admin users can access team management"""
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("bots:project-team", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 200)

    def test_project_team_view_non_admin_denied(self):
        """Test that non-admin users cannot access team management"""
        self.client.force_login(self.regular_user)

        response = self.client.get(reverse("bots:project-team", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 403)

    def test_invite_user_get_admin_access(self):
        """Test that admin users can access invite user page"""
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("bots:invite-user", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 200)

    def test_invite_user_get_non_admin_denied(self):
        """Test that non-admin users cannot access invite user page"""
        self.client.force_login(self.regular_user)

        response = self.client.get(reverse("bots:invite-user", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 403)

    def test_invite_user_post_admin_access(self):
        """Test that admin users can invite users"""
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse("bots:invite-user", kwargs={"object_id": self.project.object_id}), data={"email": "newuser@example.com", "is_admin": "false", "project_access": [self.project.object_id]})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(email="newuser@example.com").exists())

    def test_invite_user_post_non_admin_denied(self):
        """Test that non-admin users cannot invite users"""
        self.client.force_login(self.regular_user)

        response = self.client.post(reverse("bots:invite-user", kwargs={"object_id": self.project.object_id}), data={"email": "newuser@example.com", "is_admin": "false", "project_access": [self.project.object_id]})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(email="newuser@example.com").exists())

    def test_edit_user_admin_access(self):
        """Test that admin users can edit other users"""
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse("bots:edit-user", kwargs={"object_id": self.project.object_id}), data={"user_object_id": self.target_user.object_id, "is_admin": "false", "is_active": "true", "project_access": [self.project.object_id]})

        self.assertEqual(response.status_code, 200)

    def test_edit_user_non_admin_denied(self):
        """Test that non-admin users cannot edit other users"""
        self.client.force_login(self.regular_user)

        response = self.client.post(reverse("bots:edit-user", kwargs={"object_id": self.project.object_id}), data={"user_object_id": self.target_user.object_id, "is_admin": "false", "is_active": "true", "project_access": [self.project.object_id]})

        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_user_redirected_to_login(self):
        """Test that unauthenticated users are redirected to login for admin endpoints"""
        # Test without logging in any user
        admin_endpoints = [
            ("bots:create-project", {}),
            ("bots:project-project", {"object_id": self.project.object_id}),
            ("bots:project-billing", {"object_id": self.project.object_id}),
            ("bots:project-team", {"object_id": self.project.object_id}),
            ("bots:invite-user", {"object_id": self.project.object_id}),
        ]

        for url_name, kwargs in admin_endpoints:
            with self.subTest(endpoint=url_name):
                response = self.client.get(reverse(url_name, kwargs=kwargs))
                # Should redirect to login page
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)

    def test_admin_required_mixin_permission_denied_message(self):
        """Test that the AdminRequiredMixin returns appropriate permission denied message"""
        from django.http import HttpRequest

        from bots.projects_views import AdminRequiredMixin

        # Create a mock view using the AdminRequiredMixin
        class TestView(AdminRequiredMixin):
            def get(self, request):
                return HttpResponse("Success")

        # Test with regular user
        request = HttpRequest()
        request.user = self.regular_user
        request.method = "GET"

        view = TestView()

        with self.assertRaises(PermissionDenied) as cm:
            view.dispatch(request)

        self.assertEqual(str(cm.exception), "Only administrators can access this resource.")

    def test_admin_can_access_projects_they_dont_have_explicit_access_to(self):
        """Test that admin users can access any project in their organization"""
        # Create a new project that admin doesn't have explicit access to
        restricted_project = Project.objects.create(
            name="Restricted Project",
            organization=self.organization,
        )

        self.client.force_login(self.admin_user)

        # Admin should be able to access project settings even without explicit ProjectAccess
        response = self.client.get(reverse("bots:project-project", kwargs={"object_id": restricted_project.object_id}))

        self.assertEqual(response.status_code, 200)

    def test_regular_user_cannot_access_projects_without_explicit_access(self):
        """Test that regular users cannot access projects they don't have explicit access to"""
        # Create a new project that regular user doesn't have access to
        restricted_project = Project.objects.create(
            name="Restricted Project",
            organization=self.organization,
        )

        self.client.force_login(self.regular_user)

        # Regular user should get permission denied
        response = self.client.get(reverse("bots:project-project", kwargs={"object_id": restricted_project.object_id}))

        self.assertEqual(response.status_code, 403)
