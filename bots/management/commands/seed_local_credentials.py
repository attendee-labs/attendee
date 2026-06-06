import os

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Organization, User
from bots.models import BotLogin, BotLoginGroup, BotLoginPlatform, Credentials, Project


class Command(BaseCommand):
    help = "Seed local dev credentials: creates a default user/org/project and injects Google Meet bot login + Sarvam API key from .env"

    def handle(self, *args, **options):
        email = os.getenv("GOOGLE_MEET_BOT_LOGIN_EMAIL", "").strip()
        password = os.getenv("GOOGLE_MEET_BOT_LOGIN_PASSWORD", "").strip()
        sarvam_key = os.getenv("SARVAM_API_KEY", "").strip()

        if not email or not password:
            self.stdout.write(self.style.WARNING("GOOGLE_MEET_BOT_LOGIN_EMAIL or GOOGLE_MEET_BOT_LOGIN_PASSWORD not set in .env — skipping bot login creation"))

        if not sarvam_key:
            self.stdout.write(self.style.WARNING("SARVAM_API_KEY not set in .env — skipping Sarvam credential creation"))

        with transaction.atomic():
            # 1. Create or get default user + org + project
            user, user_created = User.objects.get_or_create(
                email="dev@attendee.local",
                defaults={
                    "first_name": "Dev",
                    "last_name": "User",
                    "is_active": True,
                },
            )

            if user_created:
                user.set_password("attendee123")
                user.save()  # signal creates org + project
                self.stdout.write(self.style.SUCCESS(f"Created default user: {user.email} / password: attendee123"))
            else:
                self.stdout.write(f"Using existing user: {user.email}")

            project = Project.objects.filter(organization=user.organization).first()
            if not project:
                project = Project.objects.create(name="Default Project", organization=user.organization)
                self.stdout.write(self.style.SUCCESS(f"Created default project: {project.name}"))
            else:
                self.stdout.write(f"Using existing project: {project.name}")

            # 2. Google Meet Bot Login
            if email and password:
                group, _ = BotLoginGroup.objects.get_or_create(
                    project=project,
                    platform=BotLoginPlatform.GOOGLE_MEET,
                    name="Default Google Meet Group",
                )
                login, login_created = BotLogin.objects.get_or_create(
                    group=group,
                    email=email,
                    defaults={"is_active": True},
                )
                if login_created or not login.get_credentials():
                    login.set_credentials({"password": password})
                    self.stdout.write(self.style.SUCCESS(f"Created/updated Google Meet bot login for {email}"))
                else:
                    self.stdout.write(f"Google Meet bot login already exists for {email}")

            # 3. Sarvam API Key
            if sarvam_key:
                sarvam_cred, _ = Credentials.objects.get_or_create(
                    project=project,
                    credential_type=Credentials.CredentialTypes.SARVAM,
                )
                sarvam_cred.set_credentials({"api_key": sarvam_key})
                self.stdout.write(self.style.SUCCESS("Created/updated Sarvam API credentials"))

        self.stdout.write(self.style.SUCCESS("\n=== Seed complete ==="))
        self.stdout.write(f"  User:     dev@attendee.local")
        self.stdout.write(f"  Password: attendee123")
        self.stdout.write(f"  Project:  {project.name}")
