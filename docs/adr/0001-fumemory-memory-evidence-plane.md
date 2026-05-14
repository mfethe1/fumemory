# fumemory is the Memory Evidence Plane

OpenClaw remains the top-level coordinator for task routing, gateway selection, agent orchestration, and completion decisions. fumemory is the Memory Evidence Plane: it stores canonical evidence, recallable learning, leases, federation proof, and audit records, but it does not become the authoritative OpenClaw control plane.

This keeps coordination authority in OpenClaw while letting fumemory specialize in durable proof and recall. The rejected alternative was making fumemory the swarm control plane; that would couple memory storage to scheduling authority and make deployment/readiness failures harder to reason about.
