"""OpenTelemetry tracing for the worker: one trace per run attempt (Day 17).

WHAT IT IS  The ledger is already the trace — every step row says what
            happened, when, and who wrote it. This module gives the same
            events the standard shape: a span per run attempt, child spans
            per model call and per tool dispatch, with GenAI attributes
            (model, tokens, cost) — and writes the trace id into
            steps.trace_id, the column migration 001 reserved for exactly
            this. The ledger row and the span point at each other.
GOES IN     setup(service_name) once per process; tracer() wherever a span is
            opened; current_trace_id() when appending a step.
COMES OUT   Spans. With OTEL_EXPORTER_OTLP_ENDPOINT set (e.g. a collector's
            http://host:4318), they are exported OTLP/HTTP in batches; without
            it a TracerProvider still runs so trace ids are real, but nothing
            is exported and the overhead is a few dict writes per span.
TOUCHES     Process-global OTel state, once. Nothing else.
FAILS WHEN  Never for tracing reasons: a broken exporter logs and drops spans;
            the agent must not fail because observability is down.
"""

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def setup(service_name: str) -> None:
    """Install the tracer provider once. Exports only if an endpoint is configured."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("relay")


def current_trace_id() -> str | None:
    """The active trace id as 32 hex chars, for steps.trace_id. None outside a span."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")
