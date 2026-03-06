import os
from dataclasses import dataclass

@dataclass
class OTelConfig:
    endpoint: str = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    service_name: str = os.environ.get("OTEL_SERVICE_NAME", "memu")
    sample_rate: float = float(os.environ.get("OTEL_SAMPLE_RATE", "1.0"))
