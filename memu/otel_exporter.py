import asyncio
import logging
from .otel_exporter_config import OTelConfig

logger = logging.getLogger(__name__)

async def start_otel_exporter(nats_client):
    """Stubbed OTel Exporter for memu"""
    logger.info("OTel exporter started (stubbed)")
    # TODO: Implement NATS subscription and span mappings
