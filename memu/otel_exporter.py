import asyncio
import logging
import json
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import Status, StatusCode

from memu.otel_exporter_config import load_otel_config
from memu.cluster import NATSClusterManager

logger = logging.getLogger(__name__)

class OTelExporterTask:
    def __init__(self, nats_manager: NATSClusterManager):
        self.nats_manager = nats_manager
        self.config = load_otel_config()
        self._spans = {}
        self._tracer = None
        self._setup_otel()

    def _setup_otel(self):
        try:
            resource = Resource.create({"service.name": self.config.service_name})
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=self.config.exporter_otlp_endpoint, insecure=True)
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer(__name__)
            logger.info(f"Configured OTel exporting to {self.config.exporter_otlp_endpoint}")
        except Exception as e:
            logger.error(f"Failed to setup OTel: {e}")

    async def _handle_event(self, msg):
        if not self._tracer:
            return
            
        try:
            data = json.loads(msg.data.decode())
            event_type = data.get("event_type")
            task_id = data.get("task_id")
            root_prompt_id = data.get("root_prompt_id", "unknown-root")
            
            if not task_id or not event_type:
                return

            if event_type == "task_claimed":
                span = self._tracer.start_span(f"Task: {task_id}")
                span.set_attribute("root_prompt_id", root_prompt_id)
                span.set_attribute("task_id", task_id)
                
                if "compute_cost" in data:
                    span.set_attribute("compute_cost", data["compute_cost"])
                if "fencing_token" in data:
                    span.set_attribute("fencing_token", data["fencing_token"])
                if "gateway_id" in data:
                    span.set_attribute("gateway_id", data["gateway_id"])
                if "agent_id" in data:
                    span.set_attribute("agent_id", data["agent_id"])
                
                self._spans[task_id] = span

            elif event_type in ["task_completed", "task_failed"]:
                span = self._spans.pop(task_id, None)
                if span:
                    if event_type == "task_failed":
                        span.set_status(Status(StatusCode.ERROR))
                        error_msg = data.get("error", "Unknown error")
                        span.record_exception(Exception(error_msg))
                    else:
                        span.set_status(Status(StatusCode.OK))
                        
                    if "compute_cost" in data:
                        span.set_attribute("compute_cost", data["compute_cost"])
                        
                    span.end()
                    
        except Exception as e:
            logger.error(f"Error handling OTel event: {e}")

    async def start(self):
        try:
            nc = await self.nats_manager.get_client()
            await nc.subscribe("swarm.events.>", cb=self._handle_event)
            logger.info("OTel Exporter subscribed to swarm.events.>")
        except Exception as e:
            logger.error(f"Failed to start OTel Exporter: {e}")
