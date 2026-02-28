# memu/nats_worker.py

import asyncio
import os
import json
import logging
import signal
from nats.aio.client import Client as NATS

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("memu.worker")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")

async def run():
    nc = NATS()
    
    try:
        await nc.connect(servers=[NATS_URL])
        logger.info(f"Connected to NATS at {NATS_URL}")
    except Exception as e:
        logger.error(f"Failed to connect to NATS: {e}")
        return

    async def message_handler(msg):
        subject = msg.subject
        reply = msg.reply
        data = msg.data.decode()
        logger.info(f"Received message on '{subject}': {data}")
        
        try:
            payload = json.loads(data)
            # Process payload (e.g., store in DB, update Notion)
            # This is where the Notion Sync logic would live
            
            # Ack if JetStream
            # await msg.ack()
        except json.JSONDecodeError:
            logger.error("Invalid JSON payload")

    # Subscribe to relevant subjects
    await nc.subscribe("agent.heartbeat", cb=message_handler)
    await nc.subscribe("agent.action", cb=message_handler)
    await nc.subscribe("agent.search", cb=message_handler)

    logger.info("Listening for messages...")
    
    # Keep running until interrupted
    stop_event = asyncio.Event()
    
    def signal_handler():
        stop_event.set()
        
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)
    
    await stop_event.wait()
    
    await nc.drain()
    await nc.close()
    logger.info("Worker stopped.")

if __name__ == "__main__":
    asyncio.run(run())
