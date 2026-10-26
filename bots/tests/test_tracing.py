import os
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from attendee.telemetry import TRACES_ENDPOINT_VARIABLES, init_tracing, tracer
from bots.bots_api_utils import BotCreationSource, create_bot
from bots.models import Bot, BotStates, Organization, Project, WebhookSecret, WebhookSubscription, WebhookTriggerTypes
from bots.webhook_utils import trigger_webhook

LAUNCH_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
LAUNCH_SPAN_ID = "00f067aa0ba902b7"


def set_tracer_provider(provider):
    # OpenTelemetry sets its global provider once per process; this resets it the way
    # opentelemetry-test-utils does, so each test starts with tracing on or off as it needs
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace._TRACER_PROVIDER = None
    if provider is not None:
        trace.set_tracer_provider(provider)


def record_spans(test_case):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    set_tracer_provider(provider)
    test_case.addCleanup(set_tracer_provider, None)
    return exporter


class InitTracingTest(TestCase):
    def test_tracing_stays_off_without_an_otlp_endpoint(self):
        set_tracer_provider(None)
        environment = {key: value for key, value in os.environ.items() if key not in TRACES_ENDPOINT_VARIABLES}
        with patch.dict(os.environ, environment, clear=True):
            init_tracing()

        self.assertNotIsInstance(trace.get_tracer_provider(), TracerProvider)


class BotTraceContextTest(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)

    def create_bot(self):
        bot, error = create_bot(data={"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot"}, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(error)
        return bot

    def test_a_bot_created_in_a_trace_keeps_it(self):
        record_spans(self)
        with tracer().start_as_current_span("POST /api/v1/bots") as span:
            bot = self.create_bot()

        bot.refresh_from_db()
        self.assertIn(format(span.get_span_context().trace_id, "032x"), bot.trace_context["traceparent"])

    def test_a_bot_created_without_tracing_keeps_nothing(self):
        set_tracer_provider(None)
        bot = self.create_bot()

        bot.refresh_from_db()
        self.assertIsNone(bot.trace_context)


class WebhookTracingTest(TransactionTestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)
        WebhookSecret.objects.create(project=self.project)
        WebhookSubscription.objects.create(project=self.project, url="https://example.com/webhook", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])
        self.bot = Bot.objects.create(
            project=self.project,
            meeting_url="https://zoom.us/j/123",
            state=BotStates.READY,
            trace_context={"traceparent": f"00-{LAUNCH_TRACE_ID}-{LAUNCH_SPAN_ID}-01"},
        )

        from django.conf import settings

        settings.CELERY_TASK_ALWAYS_EAGER = True
        settings.CELERY_TASK_EAGER_PROPAGATES = True

    def trigger(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "OK"
        trigger_webhook(WebhookTriggerTypes.BOT_STATE_CHANGE, bot=self.bot, payload={"state": "joined"})
        mock_post.assert_called_once()
        return mock_post.call_args.kwargs["headers"]

    @patch("bots.tasks.deliver_webhook_task.requests.post")
    def test_a_bot_webhook_links_to_the_bots_trace_and_sends_its_own(self, mock_post):
        spans = record_spans(self)

        headers = self.trigger(mock_post)

        (trigger_span,) = [span for span in spans.get_finished_spans() if span.name == "trigger_webhook"]
        self.assertEqual(trigger_span.attributes["attendee.webhook.trigger"], WebhookTriggerTypes.BOT_STATE_CHANGE)
        (link,) = trigger_span.links
        self.assertEqual(format(link.context.trace_id, "032x"), LAUNCH_TRACE_ID)
        self.assertEqual(format(link.context.span_id, "016x"), LAUNCH_SPAN_ID)
        # The delivery continues the trigger's trace, so the receiver can continue it too
        self.assertIn(format(trigger_span.context.trace_id, "032x"), headers["traceparent"])

    @patch("bots.tasks.deliver_webhook_task.requests.post")
    def test_without_tracing_a_webhook_sends_no_traceparent(self, mock_post):
        set_tracer_provider(None)

        headers = self.trigger(mock_post)

        self.assertNotIn("traceparent", headers)
