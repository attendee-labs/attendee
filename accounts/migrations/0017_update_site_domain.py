import os

from django.db import migrations


def update_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    site_domain = os.getenv("SITE_DOMAIN", "oppy.pro")
    Site.objects.update_or_create(
        id=1,
        defaults={
            "domain": site_domain,
            "name": "Attendee",
        },
    )


def reverse_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.filter(id=1).update(domain="example.com", name="example.com")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0016_backfill_is_managed_zoom_oauth_enabled"),
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [
        migrations.RunPython(update_site_domain, reverse_site_domain),
    ]
