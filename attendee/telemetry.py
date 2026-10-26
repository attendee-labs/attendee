import os

from opentelemetry import context, propagate, trace

TRACES_ENDPOINT_VARIABLES = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")


def tracer():
    # Looked up on each use rather than kept: a kept tracer binds to the first provider it sees for good
    return trace.get_tracer("attendee")


def tracing_enabled():
    return any(os.getenv(variable) for variable in TRACES_ENDPOINT_VARIABLES)


def init_tracing():
    """
    Export OpenTelemetry traces over OTLP/HTTP. Opt-in: does nothing unless an OTLP endpoint
    environment variable is set. The standard OTEL_* variables configure the exporter.
    """
    if not tracing_enabled():
        return

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # OTEL_SERVICE_NAME, when set, wins over the default name
    attributes = {} if os.getenv("OTEL_SERVICE_NAME") else {SERVICE_NAME: "attendee"}
    provider = TracerProvider(resource=Resource.create(attributes))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    DjangoInstrumentor().instrument()
    CeleryInstrumentor().instrument()


BOT_ID_ATTRIBUTE = "attendee.bot_id"


def tag_current_span_with_bot(bot):
    """Label the active span with the bot's id, so one search finds every trace of a meeting."""
    trace.get_current_span().set_attribute(BOT_ID_ATTRIBUTE, bot.object_id)


def inject_trace_headers(headers):
    propagate.inject(headers)
    return headers


def run_in_context(saved_context, function, *args):
    """Run function under a context captured earlier, e.g. in a transaction.on_commit callback."""
    token = context.attach(saved_context)
    try:
        return function(*args)
    finally:
        context.detach(token)
