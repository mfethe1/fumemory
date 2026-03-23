# HEARTBEAT.md — Periodic Check-In Template

## Checklist
- [ ] Review recent daily logs → promote to MEMORY.md
- [ ] Sync findings to shared memory (memU)
- [ ] Check for unread messages/notifications
- [ ] NATS local health: `curl -fsS http://localhost:8222/healthz`
- [ ] NATS Railway TCP reachable: `nc -z maglev.proxy.rlwy.net 55041`
- [ ] JetStream API errors: `curl -fsS http://localhost:8222/jsz | jq '.api.errors'`
- [ ] memU Railway health: `curl -fsS https://api-production-86f5.up.railway.app/api/v1/memu/health`
