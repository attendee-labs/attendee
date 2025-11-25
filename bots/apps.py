from django.apps import AppConfig


class BotsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bots"

    def ready(self):
        # Import checks to register them with Django's check framework
        from bots import checks  # noqa: F401
