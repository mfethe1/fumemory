# memU Best Practices for Agents

1. **Tagging:** Always include specific project tags (`#issueflow`, `#buildbid`) in memory content.
2. **Atomic Memories:** Store one fact per memory. Don't bulk 10 decisions into a single string.
3. **Retrieval Weighting:** When querying, prefer `temporal_weight=true` for recent project status, but `temporal_weight=false` for core business facts (API keys, preferences).
4. **Agent ID:** Always attach your agent ID so we know who learned what.
