# Graph-Lite Quick Start Guide

## 5-Minute Overview

Graph-Lite enables you to tag memories with entity relationships, creating a lightweight knowledge graph.

## Basic Usage

### 1. Create Memory with Explicit Link

```python
from memu.models import MemoryCreate, Relationship, MemoryType

# First, create a base memory
base = MemoryCreate(
    content="Python is a programming language",
    memory_type=MemoryType.fact,
    agent_id="my_agent"
)
# Store it and get back base_id

# Then create a related memory
related = MemoryCreate(
    content="Django is a Python web framework",
    memory_type=MemoryType.fact,
    agent_id="my_agent",
    relationships=[
        Relationship(
            entity="Python",
            relationship_type="extends",
            target_memory_id=base_id,  # Link to base memory
            strength=0.9
        )
    ]
)
```

### 2. Create Memory with Entity Tags (No Target)

```python
# Tag entities for future linking
memory = MemoryCreate(
    content="FastAPI is great for building APIs",
    memory_type=MemoryType.observation,
    agent_id="my_agent",
    relationships=[
        Relationship(
            entity="Python",
            relationship_type="related",
            strength=0.8
        ),
        Relationship(
            entity="API",
            relationship_type="related",
            strength=0.9
        )
    ]
)
```

## API Usage

### Create Memory with Relationships

```bash
curl -X POST http://localhost:8000/memories \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Redis is an in-memory database",
    "memory_type": "fact",
    "agent_id": "my_agent",
    "relationships": [
      {
        "entity": "database",
        "relationship_type": "related",
        "strength": 0.8
      }
    ]
  }'
```

## Relationship Types

| Type | Use When |
|------|----------|
| `similar` | Memories have similar meaning |
| `extends` | One builds upon another |
| `contradicts` | Information conflicts |
| `supersedes` | Newer replaces older |
| `caused_by` | Causal relationship |
| `related` | General association |

## Query Examples

### Find Related Memories

```sql
-- Find all memories linked to a specific memory
SELECT m.content, ml.relationship, ml.strength
FROM memories m
JOIN memory_links ml ON m.id = ml.target_id
WHERE ml.source_id = 'your-memory-uuid';
```

### Find by Entity

```sql
-- Find memories tagged with "Python"
SELECT content
FROM memories
WHERE metadata @> '{"entities": [{"name": "Python"}]}';
```

## Testing

```bash
# Quick validation (no dependencies)
cd fumemory
python3 eval_graph_lite.py

# Full validation
python3 validate_implementation.py
```

## Common Patterns

### Pattern 1: Building a Knowledge Chain

```python
# Memory 1: Base concept
base = MemoryCreate(content="HTTP is a protocol", ...)

# Memory 2: Extends base
extended = MemoryCreate(
    content="REST uses HTTP for APIs",
    relationships=[
        Relationship(
            entity="HTTP",
            relationship_type="extends",
            target_memory_id=base_id,
            strength=0.9
        )
    ]
)

# Memory 3: Further extension
advanced = MemoryCreate(
    content="GraphQL is an alternative to REST",
    relationships=[
        Relationship(
            entity="REST",
            relationship_type="related",
            target_memory_id=extended_id,
            strength=0.7
        )
    ]
)
```

### Pattern 2: Tagging Multiple Entities

```python
memory = MemoryCreate(
    content="PostgreSQL with pgvector enables semantic search",
    relationships=[
        Relationship(entity="PostgreSQL", relationship_type="related", strength=0.9),
        Relationship(entity="pgvector", relationship_type="related", strength=0.9),
        Relationship(entity="semantic_search", relationship_type="related", strength=0.8),
    ]
)
```

### Pattern 3: Updating Information

```python
# Old information
old = MemoryCreate(content="Python 2 is the standard", ...)

# New information supersedes old
new = MemoryCreate(
    content="Python 3 is the current standard",
    relationships=[
        Relationship(
            entity="Python 2",
            relationship_type="supersedes",
            target_memory_id=old_id,
            strength=1.0
        )
    ]
)
```

## Best Practices

1. **Use Explicit Links When Possible**: If you know the target memory UUID, use it
2. **Set Appropriate Strength**: Use 0.9-1.0 for strong relationships, 0.5-0.7 for weak ones
3. **Choose Correct Type**: Use specific types (extends, supersedes) over generic (related)
4. **Tag Entities Liberally**: When in doubt, add entity tags for future linking
5. **Keep Entity Names Consistent**: Use the same name format (e.g., "Python" not "python" or "Python3")

## Troubleshooting

### Issue: Relationships not being stored

**Check**: Ensure `relationships` is a list, not a dict
```python
# ✅ Correct
relationships=[Relationship(...)]

# ❌ Wrong
relationships=Relationship(...)
```

### Issue: Target memory not found

**Check**: Verify the target_memory_id exists
```sql
SELECT id FROM memories WHERE id = 'your-uuid';
```

### Issue: Duplicate relationship error

**Solution**: The system automatically handles duplicates by strengthening existing links. This is expected behavior.

## More Information

- Full documentation: `fumemory/docs/GRAPH_LITE_RELATIONSHIPS.md`
- Implementation details: `fumemory/GRAPH_LITE_IMPLEMENTATION.md`
- Delivery summary: `GRAPH_LITE_DELIVERY.md`

