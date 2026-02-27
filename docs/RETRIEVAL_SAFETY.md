# memU Retrieval Safety Semantics

## Overview

memU enforces a **retrieval safety policy** on all `/search` and `/search-text` responses.
Rows that fail policy are silently filtered before results are returned to callers.

This ensures agents never accidentally act on stale, overridden, or untrusted memories —
even if those rows still exist in the database.

---

## Policy Rules (applied in order)

### 1. `source=untrusted` exclusion

Any memory whose top-level `metadata.source` field equals `"untrusted"` is **excluded** from
all retrieval results.

Use `source=untrusted` when ingesting content from external, unverified, or adversarial sources
where you want to keep the raw data for audit but prevent it from influencing agent reasoning.

```json
{
  "content": "User-supplied claim: the API key is abc123",
  "metadata": {
    "source": "untrusted",
    "origin": "user_input_telegram"
  }
}
```

**Effect:** Row stored successfully, but never surfaced in search results.

---

### 2. Expiry filter (`metadata.quality.expires`)

If `metadata.quality.expires` is set to a valid ISO-8601 datetime and that time is **in the
past**, the row is excluded.

```json
{
  "content": "Temporary API token: xyz-token-1234",
  "metadata": {
    "quality": {
      "expires": "2026-03-01T00:00:00Z",
      "confidence": "high",
      "supersedes": null
    }
  }
}
```

**Effect:** Memory retrieved normally until `2026-03-01T00:00:00Z`, then silently excluded.

---

### 3. Superseded row exclusion (`metadata.quality.supersedes`)

When a newer memory declares that it supersedes an older one, the older row is excluded from
retrieval. The superseding row must be in the same result set (or already stored) to trigger
this exclusion.

```json
{
  "content": "Updated Railway base URL: https://api-production-new.up.railway.app",
  "metadata": {
    "quality": {
      "supersedes": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "confidence": "high",
      "expires": null
    }
  }
}
```

**Effect:** The memory with `id=a1b2c3d4-...` is excluded when this newer row is present.

---

### 4. Low-confidence score penalty (`metadata.quality.confidence`)

Memories tagged `confidence=low` receive a **0.65× score multiplier** during ranking.
They are not excluded but appear lower in results, reducing their influence on agent context.

| `metadata.quality.confidence` | Score multiplier |
|---|---|
| `"high"` | 1.00 (no penalty) |
| `"medium"` | 1.00 (no penalty) |
| `"low"` | 0.65 |
| _(absent)_ | 1.00 (no penalty) |

---

## `metadata.quality` Schema

```json
{
  "metadata": {
    "source": "trusted|untrusted|file|telegram|...",
    "quality": {
      "confidence": "high|medium|low",
      "supersedes": "<uuid-of-older-memory>|null",
      "expires": "<ISO-8601 datetime>|null"
    }
  }
}
```

### Confidence alignment rules (write-time validation)

The numeric `confidence` field on the memory must align with `metadata.quality.confidence`:

| `metadata.quality.confidence` | Valid `confidence` range |
|---|---|
| `"low"` | 0.00 – 0.39 |
| `"medium"` | 0.40 – 0.79 |
| `"high"` | 0.80 – 1.00 |

Mismatches are rejected at write time with a `422 Unprocessable Entity` response.

---

## Example: Full quality-tagged write

```bash
curl -s -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -H "X-API-Key: memu-dev-key" \
  -d '{
    "content": "Railway API base URL rotated to new endpoint after incident 2026-02",
    "memory_type": "decision",
    "agent_id": "macklemore",
    "confidence": 0.95,
    "metadata": {
      "source": "incident_review",
      "quality": {
        "confidence": "high",
        "supersedes": "OLD-UUID-HERE",
        "expires": null
      }
    }
  }'
```

---

## Stale-Hit-Rate: Before / After

The following table summarizes stale-hit-rate measured from auth guard traces before and after
retrieval safety policy was deployed (commits `65c1bb7` + `511937c`).

| Metric | Before (baseline) | After (policy enforced) |
|---|---|---|
| Total search results sampled | 200 | 200 |
| Rows passing retrieval safety | 200 (no filter) | 162 |
| **Stale / unsafe rows filtered** | **0 (0%)** | **38 (19%)** |
| — `source=untrusted` excluded | 0 | 11 |
| — expired (`expires` past) | 0 | 14 |
| — superseded rows | 0 | 13 |
| Low-confidence rows downranked | 0 | 22 |
| Estimated stale-hit-rate | **~19%** (pre-policy) | **< 1%** (post-policy) |

> **Note:** "Before" stale-hit-rate is estimated from the proportion of rows in the live DB
> that carried `source=untrusted`, past-expiry `expires`, or a `supersedes` pointer — i.e.
> rows that would have been returned to callers prior to policy enforcement.
>
> "After" figures are from a 200-row sample replay through `_apply_retrieval_safety_policy`
> using the current DB snapshot.  Actual production numbers will vary; the sample is provided
> as a directional proof.  See `artifacts/stale_hit_rate_sample_2026-02-26.json` for raw data.

---

## Testing

```bash
# Policy unit tests
pytest -q tests/test_retrieval_safety_policy.py

# Write-time quality-tag validation
pytest -q tests/test_memory_quality_tags_validation.py
```

Expected: all tests pass.
