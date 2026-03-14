"""Manual integration script for Graph-Lite entity/relationship tagging.

This requires a live database and should not run in the default baseline/CI suite.
Run it directly when you want graph-lite storage/traversal proof.
"""

import asyncio
import asyncpg
import os
import sys
from uuid import uuid4, UUID
import json
import pytest

pytestmark = pytest.mark.skip(reason="manual integration script; requires live memU database")

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from memu.models import MemoryCreate, MemoryType, Relationship

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://memu:memu@localhost:5432/memu")


async def test_graph_lite_relationships():
    """Main test function for Graph-Lite relationships."""
    
    print("=" * 80)
    print("Graph-Lite Entity & Relationship Tagging - Evaluation Script")
    print("=" * 80)
    
    # Connect to database
    conn = await asyncpg.connect(DATABASE_URL)
    
    try:
        # Clean up test data
        await conn.execute("DELETE FROM memory_links WHERE metadata->>'test_run' = 'graph_lite_eval'")
        await conn.execute("DELETE FROM memories WHERE agent_id = 'graph_lite_test_agent'")
        
        print("\n✓ Test environment cleaned")
        
        # Test 1: Create base memory
        print("\n[Test 1] Creating base memory...")
        base_memory_id = await conn.fetchval(
            """
            INSERT INTO memories (content, memory_type, agent_id, metadata)
            VALUES ($1, $2, $3, $4::jsonb)
            RETURNING id
            """,
            "Python is a high-level programming language",
            "fact",
            "graph_lite_test_agent",
            json.dumps({"test_run": "graph_lite_eval"})
        )
        print(f"✓ Base memory created: {base_memory_id}")
        
        # Test 2: Create memory with relationships (with target_memory_id)
        print("\n[Test 2] Creating memory with explicit relationship link...")
        related_memory_id = await conn.fetchval(
            """
            INSERT INTO memories (content, memory_type, agent_id, metadata)
            VALUES ($1, $2, $3, $4::jsonb)
            RETURNING id
            """,
            "Django is a Python web framework",
            "fact",
            "graph_lite_test_agent",
            json.dumps({"test_run": "graph_lite_eval"})
        )
        
        # Create relationship link
        await conn.execute(
            """
            INSERT INTO memory_links (source_id, target_id, relationship, strength, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            related_memory_id,
            base_memory_id,
            "extends",
            0.8,
            json.dumps({"entity": "Python", "test_run": "graph_lite_eval"})
        )
        print(f"✓ Related memory created: {related_memory_id}")
        print(f"✓ Relationship link created: {related_memory_id} --[extends]--> {base_memory_id}")
        
        # Test 3: Create memory with entity metadata (no target_memory_id)
        print("\n[Test 3] Creating memory with entity metadata...")
        entity_memory_id = await conn.fetchval(
            """
            INSERT INTO memories (content, memory_type, agent_id, metadata)
            VALUES ($1, $2, $3, $4::jsonb)
            RETURNING id
            """,
            "FastAPI is gaining popularity for API development",
            "observation",
            "graph_lite_test_agent",
            json.dumps({
                "test_run": "graph_lite_eval",
                "entities": [
                    {
                        "name": "Python",
                        "relationship_type": "related",
                        "strength": 0.7
                    },
                    {
                        "name": "API",
                        "relationship_type": "related",
                        "strength": 0.9
                    }
                ]
            })
        )
        print(f"✓ Entity-tagged memory created: {entity_memory_id}")
        
        # Test 4: Verify relationship storage
        print("\n[Test 4] Verifying relationship storage...")
        links = await conn.fetch(
            """
            SELECT source_id, target_id, relationship, strength, metadata
            FROM memory_links
            WHERE metadata->>'test_run' = 'graph_lite_eval'
            """
        )
        
        assert len(links) == 1, f"Expected 1 link, found {len(links)}"
        link = links[0]
        assert link['relationship'] == 'extends'
        assert link['strength'] == 0.8
        assert link['metadata']['entity'] == 'Python'
        print(f"✓ Relationship link verified: {link['relationship']} (strength: {link['strength']})")
        
        # Test 5: Verify entity metadata storage
        print("\n[Test 5] Verifying entity metadata storage...")
        entity_metadata = await conn.fetchval(
            """
            SELECT metadata
            FROM memories
            WHERE id = $1
            """,
            entity_memory_id
        )
        
        assert 'entities' in entity_metadata
        assert len(entity_metadata['entities']) == 2
        assert entity_metadata['entities'][0]['name'] == 'Python'
        print(f"✓ Entity metadata verified: {len(entity_metadata['entities'])} entities stored")
        
        # Test 6: Graph traversal
        print("\n[Test 6] Testing graph traversal...")
        related_memories = await conn.fetch(
            """
            SELECT m.id, m.content, ml.relationship, ml.strength
            FROM memories m
            JOIN memory_links ml ON m.id = ml.target_id
            WHERE ml.source_id = $1
            """,
            related_memory_id
        )
        
        assert len(related_memories) == 1
        assert related_memories[0]['relationship'] == 'extends'
        print(f"✓ Graph traversal successful: found {len(related_memories)} related memories")
        
        # Test 7: Bidirectional traversal
        print("\n[Test 7] Testing bidirectional graph traversal...")
        bidirectional = await conn.fetch(
            """
            SELECT m.id, m.content, ml.relationship, ml.strength
            FROM memories m
            JOIN memory_links ml ON (m.id = ml.source_id OR m.id = ml.target_id)
            WHERE (ml.source_id = $1 OR ml.target_id = $1) AND m.id != $1
            """,
            base_memory_id
        )
        
        assert len(bidirectional) >= 1
        print(f"✓ Bidirectional traversal successful: found {len(bidirectional)} connected memories")
        
        print("\n" + "=" * 80)
        print("✅ All Graph-Lite relationship tests passed!")
        print("=" * 80)
        
        return True
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        await conn.close()


if __name__ == "__main__":
    success = asyncio.run(test_graph_lite_relationships())
    sys.exit(0 if success else 1)

