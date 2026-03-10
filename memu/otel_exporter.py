import asyncio
import logging

from .otel_exporter_config import OTelConfig

logger = logging.getLogger(__name__)


async def start_otel_exporter(nats_client):
    """Stubbed OTel exporter for memU."""
    logger.info("OTel exporter started (stubbed)")
    while True:
        await asyncio.sleep(60)


class OTelExporterTask:
    """Minimal background-task wrapper used by API startup."""

    def __init__(self, nats_client, config: OTelConfig | None = None):
        self.nats_client = nats_client
        self.config = config or OTelConfig()

    async def start(self):
        await start_otel_exporter(self.nats_client)
