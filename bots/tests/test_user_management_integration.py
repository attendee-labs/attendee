from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Project, ProjectAccess


class UserManagementIntegrationTest(TransactionTestCase):
    """Integration tests for user management functionality"""

    def setUp(self):
        """Set up test environment"""
        # Create test organization
        self.organization = Organization.objects.create(name="Test Organization")

        # Create admin user
        self.admin_user = User.objects.create_user(username="admin", email="admin@example.com", password="testpassword123", role=UserRole.ADMIN)
        self.admin_user.organization = self.organization
        self.admin_user.save()

        # Create regular user
        self.regular_user = User.objects.create_user(username="regular", email="regular@example.com", password="testpassword123", role=UserRole.REGULAR_USER)
        self.regular_user.organization = self.organization
        self.regular_user.save()

        # Create another regular user for editing
        self.target_user = User.objects.create_user(username="target", email="target@example.com", password="testpassword123", role=UserRole.REGULAR_USER)
        self.target_user.organization = self.organization
        self.target_user.save()

        # Create test projects
        self.project1 = Project.objects.create(
            name="Test Project 1",
            organization=self.organization,
        )
        self.project2 = Project.objects.create(
            name="Test Project 2",
            organization=self.organization,
        )
        self.project3 = Project.objects.create(
            name="Test Project 3",
            organization=self.organization,
        )

        # Give regular user access to project1
        ProjectAccess.objects.create(project=self.project1, user=self.regular_user)

        # Give target user access to project1 and project2
        ProjectAccess.objects.create(project=self.project1, user=self.target_user)
        ProjectAccess.objects.create(project=self.project2, user=self.target_user)

        # Create test client
        self.client = Client()

    def test_edit_user_promote_to_admin(self):
        """Test promoting a regular user to admin"""
        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "true",
                "is_active": "true",
            },
        )

        # Verify success
        self.assertEqual(response.status_code, 200)
        self.assertIn("User target@example.com has been updated successfully", response.content.decode())
        self.assertIn("Role: administrator", response.content.decode())

        # Verify user role was updated
        self.target_user.refresh_from_db()
        self.assertEqual(self.target_user.role, UserRole.ADMIN)
        self.assertTrue(self.target_user.is_active)

        # Verify all explicit project access was removed (admins have access to all projects)
        self.assertEqual(ProjectAccess.objects.filter(user=self.target_user).count(), 0)

        # Verify admin can access all projects
        accessible_projects = Project.accessible_to(self.target_user)
        self.assertEqual(accessible_projects.count(), 3)

    def test_edit_user_demote_to_regular_user(self):
        """Test demoting an admin to regular user with specific project access"""
        # First promote target user to admin
        self.target_user.role = UserRole.ADMIN
        self.target_user.save()

        # Remove any existing project access
        ProjectAccess.objects.filter(user=self.target_user).delete()

        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "false",
                "is_active": "true",
                "project_access": [self.project1.object_id, self.project3.object_id],
            },
        )

        # Verify success
        self.assertEqual(response.status_code, 200)
        self.assertIn("User target@example.com has been updated successfully", response.content.decode())
        self.assertIn("Role: regular user", response.content.decode())

        # Verify user role was updated
        self.target_user.refresh_from_db()
        self.assertEqual(self.target_user.role, UserRole.REGULAR_USER)

        # Verify correct project access was created
        user_project_accesses = ProjectAccess.objects.filter(user=self.target_user)
        self.assertEqual(user_project_accesses.count(), 2)

        accessible_project_ids = set(user_project_accesses.values_list("project__object_id", flat=True))
        expected_project_ids = {self.project1.object_id, self.project3.object_id}
        self.assertEqual(accessible_project_ids, expected_project_ids)

        # Verify limited access for regular user
        accessible_projects = Project.accessible_to(self.target_user)
        self.assertEqual(accessible_projects.count(), 2)

    def test_edit_user_deactivate_user(self):
        """Test deactivating a user"""
        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "false",
                "is_active": "false",
                "project_access": [self.project1.object_id],
            },
        )

        # Verify success
        self.assertEqual(response.status_code, 200)
        self.assertIn("User target@example.com has been updated successfully", response.content.decode())
        self.assertIn("Status: disabled", response.content.decode())

        # Verify user was deactivated
        self.target_user.refresh_from_db()
        self.assertFalse(self.target_user.is_active)

        # Verify deactivated user cannot access any projects
        accessible_projects = Project.accessible_to(self.target_user)
        self.assertEqual(accessible_projects.count(), 0)

    def test_edit_user_reactivate_user(self):
        """Test reactivating a deactivated user"""
        # First deactivate the user
        self.target_user.is_active = False
        self.target_user.save()

        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "false",
                "is_active": "true",
                "project_access": [self.project2.object_id, self.project3.object_id],
            },
        )

        # Verify success
        self.assertEqual(response.status_code, 200)
        self.assertIn("User target@example.com has been updated successfully", response.content.decode())
        self.assertIn("Status: active", response.content.decode())

        # Verify user was reactivated
        self.target_user.refresh_from_db()
        self.assertTrue(self.target_user.is_active)

        # Verify new project access
        accessible_projects = Project.accessible_to(self.target_user)
        self.assertEqual(accessible_projects.count(), 2)

    def test_edit_user_update_project_access(self):
        """Test updating project access for a regular user"""
        self.client.force_login(self.admin_user)

        # Verify initial project access
        initial_access = ProjectAccess.objects.filter(user=self.target_user)
        self.assertEqual(initial_access.count(), 2)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "false",
                "is_active": "true",
                "project_access": [self.project3.object_id],  # Only project3
            },
        )

        # Verify success
        self.assertEqual(response.status_code, 200)

        # Verify project access was updated
        user_project_accesses = ProjectAccess.objects.filter(user=self.target_user)
        self.assertEqual(user_project_accesses.count(), 1)

        accessible_project_ids = set(user_project_accesses.values_list("project__object_id", flat=True))
        self.assertEqual(accessible_project_ids, {self.project3.object_id})

    def test_edit_user_non_admin_access_denied(self):
        """Test that non-admin users cannot edit other users"""
        self.client.force_login(self.regular_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "false",
                "is_active": "true",
                "project_access": [self.project1.object_id],
            },
        )

        # Should be denied access
        self.assertEqual(response.status_code, 403)

    def test_edit_user_cannot_edit_self(self):
        """Test that admin cannot edit their own account"""
        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.admin_user.object_id,  # Trying to edit self
                "is_admin": "false",
                "is_active": "true",
                "project_access": [self.project1.object_id],
            },
        )

        # Should fail
        self.assertEqual(response.status_code, 400)
        self.assertIn("You cannot edit your own account", response.content.decode())

    def test_edit_user_missing_user_object_id(self):
        """Test that missing user_object_id returns error"""
        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                # No user_object_id provided
                "is_admin": "false",
                "is_active": "true",
                "project_access": [self.project1.object_id],
            },
        )

        # Should fail
        self.assertEqual(response.status_code, 400)
        self.assertIn("User ID is required", response.content.decode())

    def test_edit_user_nonexistent_user(self):
        """Test that editing a non-existent user returns error"""
        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": "usr_nonexistent",  # Non-existent user ID
                "is_admin": "false",
                "is_active": "true",
                "project_access": [self.project1.object_id],
            },
        )

        # Should fail
        self.assertEqual(response.status_code, 404)

    def test_edit_user_regular_without_project_access(self):
        """Test that demoting to regular user without project access fails"""
        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "false",
                "is_active": "true",
                # No project_access provided
            },
        )

        # Should fail
        self.assertEqual(response.status_code, 400)
        self.assertIn("Please select at least one project for regular users", response.content.decode())

    def test_edit_user_invalid_project_access(self):
        """Test that providing invalid project IDs fails"""
        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "false",
                "is_active": "true",
                "project_access": ["invalid_project_id"],
            },
        )

        # Should fail
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid project selection", response.content.decode())

    def test_edit_user_cross_organization_access_denied(self):
        """Test that admin cannot edit users from other organizations"""
        # Create another organization and user
        other_org = Organization.objects.create(name="Other Organization")
        other_user = User.objects.create_user(username="other", email="other@example.com", password="testpassword123", role=UserRole.REGULAR_USER)
        other_user.organization = other_org
        other_user.save()

        self.client.force_login(self.admin_user)

        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": other_user.object_id,
                "is_admin": "false",
                "is_active": "true",
                "project_access": [self.project1.object_id],
            },
        )

        # Should fail
        self.assertEqual(response.status_code, 404)

    def test_project_team_view_admin_access(self):
        """Test that admin can access the project team view"""
        self.client.force_login(self.admin_user)

        team_url = reverse("projects:project-team", kwargs={"object_id": self.project1.object_id})
        response = self.client.get(team_url)

        # Should be successful
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "admin@example.com")
        self.assertContains(response, "regular@example.com")
        self.assertContains(response, "target@example.com")

    def test_project_team_view_regular_user_access_denied(self):
        """Test that regular users cannot access the project team view"""
        self.client.force_login(self.regular_user)

        team_url = reverse("projects:project-team", kwargs={"object_id": self.project1.object_id})
        response = self.client.get(team_url)

        # Should be denied access
        self.assertEqual(response.status_code, 403)

    def test_project_team_view_unauthenticated_access_denied(self):
        """Test that unauthenticated users cannot access the project team view"""
        team_url = reverse("projects:project-team", kwargs={"object_id": self.project1.object_id})
        response = self.client.get(team_url)

        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, f"/accounts/login/?next={team_url}")

    def test_users_with_access_method(self):
        """Test the Project.users_with_access method"""
        # Admin should have access to all projects
        project_users = self.project1.users_with_access()

        # Should include admin (has access to all) and users with explicit access
        expected_user_emails = {self.admin_user.email, self.regular_user.email, self.target_user.email}
        actual_user_emails = set(project_users.values_list("email", flat=True))
        self.assertEqual(actual_user_emails, expected_user_emails)

        # Test with a project that has no explicit user access
        project_users_3 = self.project3.users_with_access()
        # Should only include admin users and users with explicit access (none for project3)
        self.assertIn(self.admin_user, project_users_3)
        self.assertNotIn(self.regular_user, project_users_3)
        self.assertNotIn(self.target_user, project_users_3)

    def test_user_deactivation_removes_project_access(self):
        """Test that deactivating a user effectively removes their project access"""
        # Target user initially has access to 2 projects
        accessible_projects = Project.accessible_to(self.target_user)
        self.assertEqual(accessible_projects.count(), 2)

        # Deactivate the user
        self.client.force_login(self.admin_user)
        edit_url = reverse("projects:edit-user", kwargs={"object_id": self.project1.object_id})
        response = self.client.post(
            edit_url,
            {
                "user_object_id": self.target_user.object_id,
                "is_admin": "false",
                "is_active": "false",
                "project_access": [self.project1.object_id, self.project2.object_id],
            },
        )

        self.assertEqual(response.status_code, 200)

        self.target_user.refresh_from_db()
        self.assertFalse(self.target_user.is_active)

        # Verify user still has ProjectAccess records but accessible_to returns empty
        project_accesses = ProjectAccess.objects.filter(user=self.target_user)
        self.assertEqual(project_accesses.count(), 2)  # Records still exist

        # But accessible_to should return nothing due to is_active=False
        accessible_projects = Project.accessible_to(self.target_user)
        self.assertEqual(accessible_projects.count(), 0)
