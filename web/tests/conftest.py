"""Shared pytest fixtures for web/tests/.

Disables the OpenTelemetry SDK during the test run — without this,
server.py's OTLP exporter (see server.py's build_app()) repeatedly tries
and fails to connect to a nonexistent local ADOT collector sidecar
(localhost:4317, only present in the real ECS deployment — see
web_stack.py), which is harmless but noisy in test output.
"""
import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
