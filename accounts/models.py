import uuid

from concurrency.fields import IntegerVersionField
from django.contrib.auth.models import AbstractUser
from django.db import models


class Organization(models.Model):
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # These represent hundredths of a credit
    centicredits = models.IntegerField(default=500, null=False)
    version = IntegerVersionField()
    is_webhooks_enabled = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    def credits(self):
        return self.centicredits / 100

    def out_of_credits(self):
        return self.credits() < -1


class User(AbstractUser):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, null=False, related_name="users")
    invited_by = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="invited_users")

    def __str__(self):
        return self.email

    def identifier(self):
        # If they have a name, use that
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        if self.first_name:
            return self.first_name
        if self.last_name:
            return self.last_name
        # If they have an email, use that
        elif self.email:
            return self.email
        # Otherwise, use their username
        else:
            return self.username


# Only added this to create an org for the admin user
from django.db.models.signals import pre_save
from django.dispatch import receiver


@receiver(pre_save, sender=User)
def create_default_organization(sender, instance, **kwargs):
    # Only run this for new users (not updates)
    if not instance.pk and not instance.organization_id:
        from bots.models import Project

        default_org = Organization.objects.create(name=f"{instance.email}'s organization")

        # Create default project for the organization
        Project.objects.create(name=f"{instance.email}'s project", organization=default_org)

        # There's some weird stuff going on with username field
        # we don't need it for anything, so we'll just set it to a random uuid
        # that will avoid violating the unique constraint
        instance.username = str(uuid.uuid4())

        instance.organization = default_org
