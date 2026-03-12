# Graph-Lite Entity & Relationship Tagging

## Overview

Graph-Lite is a lightweight entity and relationship tagging system integrated into the memU memory system. It enables semantic traversal of entity connections via a graph structure without requiring a full graph database.

## Features

- **Relationship Arrays**: Every memory can include a `relationships` array defining connections to other memories or entities
- **Flexible Linking**: Support for both explicit memory-to-memory links and entity-based metadata tagging
- **Graph Traversal**: Query and traverse relationship graphs using standard SQL
- **Relationship Types**: Predefined relationship types including `similar`, `extends`, `contradicts`, `supersedes`, `caused_by`, and `related`
- **Strength Scoring**: Each relationship has a strength score (0.0-1.0) indicating connection confidence

## Data Model

### Relationship Model

```python
class Relationship(BaseModel):
    entity: str                          # Entity name or ID being referenced
    relationship_type: str               # Type of relationship
    target_memory_id: Optional[UUID]     # Optional: UUID of target memory if known
    strength: float = 0.5                # Relationship strength (0.0-1.0)
```

### Memory Links Table

The `memory_links` table stores explicit memory-to-memory relationships:

```sql
CREATE TABLE memory_links (
    id            UUID PRIMARY KEY,
    source_id     UUID NOT NULL REFERENCES memories(id),
    target_id     UUID NOT NULL REFERENCES memories(id),
    relationship  VARCHAR(20) NOT NULL,
    strength      FLOAT NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## Usage

### Creating Memories with Relationships

#### Example 1: Explicit Memory Link

```python
from memu.models import MemoryCreate, Relationship, MemoryType

# Create base memory
base_memory = MemoryCreate(
    content="PostgreSQL is a powerful relational database",
    memory_type=MemoryType.fact,
    agent_id="my_agent"
)

# Create related memory with explicit link
related_memory = MemoryCreate(
    content="pgvector extends PostgreSQL with vector similarity search",
    memory_type=MemoryType.fact,
    agent_id="my_agent",
    relationships=[
        Relationship(
            entity="PostgreSQL",
            relationship_type="extends",
            target_memory_id=base_memory_id,  # UUID of base memory
            strength=0.9
        )
    ]
)
```

#### Example 2: Entity Metadata (No Target Memory)

```python
# Create memory with entity tags for future linking
memory = MemoryCreate(
    content="Vector databases are optimized for similarity search",
    memory_type=MemoryType.observation,
    agent_id="my_agent",
    relationships=[
        Relationship(
            entity="pgvector",
            relationship_type="related",
            strength=0.7
        ),
        Relationship(
            entity="similarity_search",
            relationship_type="related",
            strength=0.8
        )
    ]
)
```

### API Usage

```bash
# Create memory with relationships
curl -X POST http://localhost:8000/memories \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Django is a Python web framework",
    "memory_type": "fact",
    "agent_id": "my_agent",
    "relationships": [
      {
        "entity": "Python",
        "relationship_type": "extends",
        "target_memory_id": "uuid-of-python-memory",
        "strength": 0.9
      }
    ]
  }'
```

### Querying Relationships

#### Find Related Memories

```sql
-- Find all memories related to a specific memory
SELECT m.id, m.content, ml.relationship, ml.strength
FROM memories m
JOIN memory_links ml ON m.id = ml.target_id
WHERE ml.source_id = 'your-memory-uuid';
```

#### Bidirectional Traversal

```sql
-- Find all connected memories (both directions)
SELECT m.id, m.content, ml.relationship, ml.strength
FROM memories m
JOIN memory_links ml ON (m.id = ml.source_id OR m.id = ml.target_id)
WHERE (ml.source_id = 'your-memory-uuid' OR ml.target_id = 'your-memory-uuid')
  AND m.id != 'your-memory-uuid';
```

#### Find Memories by Entity

```sql
-- Find memories tagged with a specific entity
SELECT id, content, metadata->'entities' as entities
FROM memories
WHERE metadata @> '{"entities": [{"name": "Python"}]}';
```

## Relationship Types

| Type | Description | Example |
|------|-------------|---------|
| `similar` | Memories with similar content or meaning | Two definitions of the same concept |
| `extends` | One memory builds upon another | Framework extends a language |
| `contradicts` | Memories with conflicting information | Outdated vs current information |
| `supersedes` | Newer memory replaces older one | Updated policy replaces old policy |
| `caused_by` | Causal relationship | Error caused by configuration |
| `related` | General association | Related topics or concepts |

## Testing

Run the evaluation script to verify the implementation:

```bash
cd fumemory
python3 eval_graph_lite.py
```

Run the full test suite:

```bash
python3 -m pytest tests/test_graph_lite_relationships.py -v
python3 tests/test_graph_lite_api.py  # Integration test
```

## Implementation Details

### Storage Strategy

1. **Explicit Links**: When `target_memory_id` is provided, a row is inserted into `memory_links`
2. **Entity Metadata**: When `target_memory_id` is not provided, entity information is stored in the memory's `metadata` JSONB field
3. **Deduplication**: Relationship links use `ON CONFLICT` to prevent duplicates and strengthen existing connections

### Performance Considerations

- Indexes on `source_id`, `target_id`, `relationship`, and `strength` enable fast graph traversal
- JSONB indexes on `metadata` support efficient entity queries
- Bidirectional queries use `OR` conditions to traverse in both directions

## Future Enhancements

- Automatic entity extraction from memory content
- Graph visualization endpoints
- Multi-hop relationship queries
- Relationship decay based on time and usage
- Entity resolution and deduplication

