from django.apps import AppConfig


class BotsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bots"

    def ready(self):
        from attendee.telemetry import init_tracing

        # Opt-in: does nothing unless an OTLP endpoint is set (see docs/environment-variables.md)
        init_tracing()
