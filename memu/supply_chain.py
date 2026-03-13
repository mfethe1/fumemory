"""Supply Chain and Long Lead Time Tracking.

This module provides database tools and API connectors for tracking long lead time
items in the task execution pipeline. Integrates with task orchestration to flag
dependencies that require early ordering or extended wait times.

Key Features:
- Long lead item database with lead time estimates
- Dependency chain analysis for critical path identification
- Integration with task orchestrator for proactive planning
- External API connectors for real-time supply chain data
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class LeadTimeCategory(str):
    """Standard lead time categories."""
    IMMEDIATE = "immediate"  # < 1 day
    SHORT = "short"  # 1-7 days
    MEDIUM = "medium"  # 1-4 weeks
    LONG = "long"  # 1-3 months
    CRITICAL = "critical"  # > 3 months


class LongLeadItem(BaseModel):
    """A tracked item with extended lead time."""
    
    item_id: UUID = Field(default_factory=uuid4)
    item_name: str
    item_type: str  # e.g., "hardware", "api_access", "data_source", "approval"
    
    # Lead time tracking
    estimated_lead_time_hours: float
    lead_time_category: str = LeadTimeCategory.MEDIUM
    
    # Supply chain metadata
    supplier: str | None = None
    supplier_api_endpoint: str | None = None
    last_availability_check: datetime | None = None
    in_stock: bool = True
    
    # Task integration
    required_by_tasks: list[UUID] = Field(default_factory=list)
    blocking_tasks: list[UUID] = Field(default_factory=list)
    
    # Tracking
    ordered_at: datetime | None = None
    expected_delivery: datetime | None = None
    delivered_at: datetime | None = None
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SupplyChainConnector:
    """Database and API connector for supply chain tracking."""
    
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self._cache: dict[UUID, LongLeadItem] = {}
    
    async def initialize_schema(self):
        """Create supply chain tracking tables."""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS long_lead_items (
                    item_id UUID PRIMARY KEY,
                    item_name TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    estimated_lead_time_hours FLOAT NOT NULL,
                    lead_time_category TEXT NOT NULL,
                    supplier TEXT,
                    supplier_api_endpoint TEXT,
                    last_availability_check TIMESTAMPTZ,
                    in_stock BOOLEAN DEFAULT TRUE,
                    required_by_tasks UUID[],
                    blocking_tasks UUID[],
                    ordered_at TIMESTAMPTZ,
                    expected_delivery TIMESTAMPTZ,
                    delivered_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                
                CREATE INDEX IF NOT EXISTS idx_long_lead_category 
                    ON long_lead_items(lead_time_category);
                
                CREATE INDEX IF NOT EXISTS idx_long_lead_delivery 
                    ON long_lead_items(expected_delivery) 
                    WHERE delivered_at IS NULL;
                
                CREATE INDEX IF NOT EXISTS idx_long_lead_blocking 
                    ON long_lead_items USING gin(blocking_tasks);
            """)
            
            logger.info("Supply chain schema initialized")
    
    async def register_item(self, item: LongLeadItem) -> UUID:
        """Register a new long lead item."""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO long_lead_items (
                    item_id, item_name, item_type, estimated_lead_time_hours,
                    lead_time_category, supplier, supplier_api_endpoint,
                    in_stock, required_by_tasks, blocking_tasks
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (item_id) DO UPDATE SET
                    updated_at = NOW(),
                    required_by_tasks = EXCLUDED.required_by_tasks,
                    blocking_tasks = EXCLUDED.blocking_tasks
            """, 
                item.item_id,
                item.item_name,
                item.item_type,
                item.estimated_lead_time_hours,
                item.lead_time_category,
                item.supplier,
                item.supplier_api_endpoint,
                item.in_stock,
                item.required_by_tasks,
                item.blocking_tasks,
            )
        
        self._cache[item.item_id] = item
        logger.info(f"Registered long lead item: {item.item_name} ({item.lead_time_category})")
        return item.item_id
    
    async def get_item(self, item_id: UUID) -> LongLeadItem | None:
        """Retrieve a long lead item by ID."""
        # Check cache first
        if item_id in self._cache:
            return self._cache[item_id]
        
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM long_lead_items WHERE item_id = $1",
                item_id
            )
        
        if not row:
            return None
        
        item = LongLeadItem(**dict(row))
        self._cache[item_id] = item
        return item

    async def get_items_for_task(self, task_id: UUID) -> list[LongLeadItem]:
        """Get all long lead items required by a specific task."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM long_lead_items
                WHERE $1 = ANY(required_by_tasks)
                ORDER BY estimated_lead_time_hours DESC
            """, task_id)

        return [LongLeadItem(**dict(row)) for row in rows]

    async def get_blocking_items(self, task_id: UUID) -> list[LongLeadItem]:
        """Get items that are currently blocking a task."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM long_lead_items
                WHERE $1 = ANY(blocking_tasks)
                  AND delivered_at IS NULL
                ORDER BY expected_delivery ASC NULLS LAST
            """, task_id)

        return [LongLeadItem(**dict(row)) for row in rows]

    async def mark_ordered(
        self,
        item_id: UUID,
        expected_delivery_hours: float | None = None
    ) -> bool:
        """Mark an item as ordered and set expected delivery."""
        item = await self.get_item(item_id)
        if not item:
            return False

        ordered_at = datetime.now(timezone.utc)

        # Calculate expected delivery
        if expected_delivery_hours is None:
            expected_delivery_hours = item.estimated_lead_time_hours

        expected_delivery = ordered_at + timedelta(hours=expected_delivery_hours)

        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE long_lead_items
                SET ordered_at = $1,
                    expected_delivery = $2,
                    updated_at = NOW()
                WHERE item_id = $3
            """, ordered_at, expected_delivery, item_id)

        logger.info(
            f"Item {item.item_name} ordered, expected delivery: {expected_delivery}"
        )
        return True

    async def mark_delivered(self, item_id: UUID) -> bool:
        """Mark an item as delivered and unblock dependent tasks."""
        async with self.db_pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE long_lead_items
                SET delivered_at = NOW(),
                    updated_at = NOW()
                WHERE item_id = $1
                  AND delivered_at IS NULL
            """, item_id)

        if result == "UPDATE 0":
            return False

        item = await self.get_item(item_id)
        if item:
            logger.info(
                f"Item {item.item_name} delivered, unblocking {len(item.blocking_tasks)} tasks"
            )

        return True

    async def check_critical_path(self, task_id: UUID) -> dict[str, Any]:
        """Analyze critical path for a task based on long lead items."""
        items = await self.get_items_for_task(task_id)

        if not items:
            return {
                "has_long_lead_items": False,
                "critical_path_hours": 0,
                "items": [],
            }

        # Find longest lead time
        max_lead_time = max(item.estimated_lead_time_hours for item in items)

        # Identify critical items (those on the critical path)
        critical_items = [
            item for item in items
            if item.estimated_lead_time_hours >= max_lead_time * 0.8
        ]

        # Check if any are not yet ordered
        unordered = [item for item in critical_items if item.ordered_at is None]

        return {
            "has_long_lead_items": True,
            "critical_path_hours": max_lead_time,
            "total_items": len(items),
            "critical_items": len(critical_items),
            "unordered_critical_items": len(unordered),
            "items": [
                {
                    "item_id": str(item.item_id),
                    "item_name": item.item_name,
                    "lead_time_hours": item.estimated_lead_time_hours,
                    "category": item.lead_time_category,
                    "ordered": item.ordered_at is not None,
                    "delivered": item.delivered_at is not None,
                }
                for item in critical_items
            ],
            "recommendation": (
                f"Order {len(unordered)} critical items immediately to avoid delays"
                if unordered
                else "All critical items ordered or delivered"
            ),
        }

    async def get_overdue_items(self) -> list[LongLeadItem]:
        """Get items that are past their expected delivery date."""
        now = datetime.now(timezone.utc)

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM long_lead_items
                WHERE expected_delivery < $1
                  AND delivered_at IS NULL
                ORDER BY expected_delivery ASC
            """, now)

        return [LongLeadItem(**dict(row)) for row in rows]

