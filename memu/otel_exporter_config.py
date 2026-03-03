import os

class OTelConfig:
    def __init__(self):
        self.service_name = os.getenv("OTEL_SERVICE_NAME", "memu")
        self.exporter_otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        self.sample_rate = float(os.getenv("OTEL_SAMPLE_RATE", "1.0"))

def load_otel_config() -> OTelConfig:
    return OTelConfig()
