# Multi-Machine Setup

FuMemory works across machines using [Tailscale](https://tailscale.com) (free mesh VPN).

## Topology

```
┌─────────────────────┐     ┌─────────────────────┐
│  GPU Machine         │     │  Server / VPS        │
│  - Ollama            │     │  - PostgreSQL        │
│  - :11434            │     │  - memU API (:8100)  │
│  Tailscale: 100.x.x.1│    │  Tailscale: 100.x.x.2│
└─────────────────────┘     └─────────────────────┘
         ▲                           ▲
         └───────────┬───────────────┘
              ┌──────┴──────┐
              │  Agent Host  │
              │  100.x.x.3  │
              └──────────────┘
```

## Steps

1. Install Tailscale on all machines
2. Host Ollama on your GPU machine (set `OLLAMA_HOST=0.0.0.0`)
3. Pull embedding model: `ollama pull qwen3-embedding`
4. Deploy memU API pointing to Ollama's Tailscale IP
5. Agents connect via `MEMU_BASE_URL=http://<api-ip>:8100`

## Security

- Tailscale traffic is encrypted end-to-end
- `x-api-key` header for application-layer auth
- Never expose Ollama or Postgres to public internet
