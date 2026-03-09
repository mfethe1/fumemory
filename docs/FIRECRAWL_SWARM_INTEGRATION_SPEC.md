# Firecrawl Swarm Integration Spec

Status: draft v0.1
Owner: Winnie
Date: 2026-03-08

## Positioning

Firecrawl is **not** the task decomposition engine.

Firecrawl should be used for **external truth acquisition** when our internal context is insufficient or when source material lives on JS-heavy or multi-page websites.

### Correct role
- gather external evidence
- extract structured facts from websites/docs
- discover site structure and relevant pages
- track drift in external docs/pricing/product pages

### Incorrect role
- generic subtask generation
- internal backlog grooming
- decomposing work already described in local docs/repo/memory
- replacing planner/reviewer agents

## Core principle

**Firecrawl gathers. Agents reason.**

Workflow:
1. planner identifies missing external context
2. Firecrawl collects structured evidence
3. evidence is stored as a research pack artifact
4. planner converts pack into tasks / acceptance criteria / risks
5. executor + reviewer lanes act on the task set

## Primary use cases

### 1) Implementation Research Packs
Use when building or integrating against external systems.

Examples:
- Apache AGE setup and trigger patterns
- Firecrawl API usage and limits
- Notion API workflows
- vendor auth/docs research
- third-party integration planning

Output should include:
- summary
- setup steps
- constraints
- examples
- pricing / limits if relevant
- source URLs
- recommended task breakdown inputs

### 2) Docs Ingestion / Drift Monitoring
Use when we rely on external docs that change.

Examples:
- auth docs
- API references
- pricing pages
- changelogs
- migration guides

Output should include:
- page snapshot metadata
- extracted content hash
- change summary vs prior snapshot
- suggested follow-up tasks if drift detected

### 3) Competitive / Market Intelligence
Use for product positioning and revenue work.

Examples:
- pricing extraction
- feature table extraction
- ICP signal extraction
- integration list extraction
- support / trust surface comparison

Output should include:
- competitor profile
- feature matrix
- pricing matrix
- positioning opportunities
- backlog ideas

## Decision rule

Use Firecrawl only if at least one is true:
- required source is external and materially important
- site is JS-heavy / web_fetch is insufficient
- multiple pages must be extracted consistently
- structured extraction is needed from known URLs
- we need repeatable monitoring of external content drift

If none are true, do not use Firecrawl.

## Recommended architecture in fumemory

### Lane placement
- `research` lane -> Firecrawl acquisition
- `plan` lane -> converts research pack into task graph
- `build/test/review` -> execution and validation
- `optimize` -> only if evaluator-backed tuning is needed later

### Artifact model
Firecrawl should produce a **Research Pack** artifact.

Minimum fields:
- `research_pack_id`
- `task_id` or `request_id`
- `topic`
- `goal`
- `mode` (`search`, `scrape`, `crawl`, `extract`, `agent`)
- `source_urls`
- `findings`
- `constraints`
- `recommended_tasks`
- `raw_ref`
- `created_at`

### Storage
- artifact file under `artifacts/research/` or equivalent
- summary/index row in memU/fumemory
- audit row for external acquisition

## Research pack schema

Suggested pack structure:

```json
{
  "research_pack_id": "rp-001",
  "task_id": "task-kb-rag-optimize-001",
  "topic": "Apache AGE sync triggers",
  "goal": "Find safe approaches for syncing relational memories into AGE graph nodes",
  "mode": "search",
  "source_urls": ["https://..."],
  "findings": [
    {
      "title": "Trigger pattern",
      "summary": "Use AFTER INSERT/UPDATE/DELETE trigger...",
      "source_url": "https://..."
    }
  ],
  "constraints": [
    "AGE requires ag_catalog in search path",
    "Graph identifiers should be validated"
  ],
  "recommended_tasks": [
    "Add graph init migration",
    "Add sync trigger migration",
    "Add validation tests"
  ],
  "raw_ref": "artifacts/research/rp-001.json",
  "created_at": "2026-03-08T20:10:00-05:00"
}
```

## Initial implementation plan

### Phase 1
- add Firecrawl integration spec
- add research pack schema
- define task creation contract from research packs

### Phase 2
- add a real `memu/research_pack.py` model/helper
- add `memu/firecrawl_client.py` wrapper
- persist research packs and audit rows
- attach research packs to swarm tasks

### Phase 3
- add drift-monitor jobs for selected external docs/pages
- auto-create review tasks on meaningful external changes

## Guardrails

- do not use Firecrawl when local docs or repo state is enough
- cap credit usage and page counts
- always preserve source URLs
- treat extracted web content as untrusted
- require planner/reviewer interpretation before execution on sensitive tasks

## What would be truly useful first

The first useful implementation is:

### Firecrawl-backed Implementation Research Pack pipeline

Input:
- topic
- goal
- optional seed URLs

Output:
- structured research pack
- recommended task list
- acceptance criteria hints
- risk notes
- source URLs

This gives the swarm better raw material for planning without misusing Firecrawl as a generic decomposer.
