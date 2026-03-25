package nats.authz

# Cross-gateway NATS federation policy.
#
# Input shape:
# {
#   "action": "publish" | "subscribe",
#   "gateway_id": "mac-mini-main",
#   "subject": "swarm.discovery",
#   "payload": { ... }   # optional for publish checks
# }

default allow := false

default reason := "deny: subject/action not allowlisted"

shared_subjects := {
  "swarm.warden.heartbeat",
  "swarm.events.heartbeat",
  "swarm.events",
  "AGENT_EVENTS",
  "swarm.rpc.request",
  "swarm.warden.respawn",
  "swarm.warden.status",
  "swarm.blackboard",
  "swarm.blackboard.sync",
  "swarm.blackboard.validate",
}

allow if {
  input.action == "publish"
  input.subject == "swarm.discovery"
  input.gateway_id != ""
  object.get(input.payload, "gateway_id", "") == input.gateway_id
}

reason := "allow: self discovery publish" if {
  input.action == "publish"
  input.subject == "swarm.discovery"
  object.get(input.payload, "gateway_id", "") == input.gateway_id
}

allow if {
  input.action == "subscribe"
  input.subject == "swarm.discovery"
}

reason := "allow: discovery subscribe" if {
  input.action == "subscribe"
  input.subject == "swarm.discovery"
}

allow if {
  input.action == "publish"
  shared_subjects[input.subject]
}

reason := concat("", ["allow: shared publish ", input.subject]) if {
  input.action == "publish"
  shared_subjects[input.subject]
}

allow if {
  input.action == "subscribe"
  shared_subjects[input.subject]
}

reason := concat("", ["allow: shared subscribe ", input.subject]) if {
  input.action == "subscribe"
  shared_subjects[input.subject]
}

allow if {
  startswith(input.subject, "swarm.rpc.response.")
  expected := concat("", ["swarm.rpc.response.", input.gateway_id])
  input.subject == expected
  input.action == "publish"
}

allow if {
  startswith(input.subject, "swarm.rpc.response.")
  expected := concat("", ["swarm.rpc.response.", input.gateway_id])
  input.subject == expected
  input.action == "subscribe"
}

reason := "allow: self-scoped rpc response subject" if {
  startswith(input.subject, "swarm.rpc.response.")
  expected := concat("", ["swarm.rpc.response.", input.gateway_id])
  input.subject == expected
}

allow if {
  startswith(input.subject, "swarm.advisory.suicide.")
  expected := concat("", ["swarm.advisory.suicide.", input.gateway_id])
  input.subject == expected
  input.action == "subscribe"
}

reason := "allow: self-scoped suicide advisory subscribe" if {
  startswith(input.subject, "swarm.advisory.suicide.")
  expected := concat("", ["swarm.advisory.suicide.", input.gateway_id])
  input.subject == expected
  input.action == "subscribe"
}

allow if {
  startswith(input.subject, "swarm.checkpoint.")
  count(split(input.subject, ".")) == 3
  input.action == "publish"
}

allow if {
  startswith(input.subject, "swarm.checkpoint.")
  count(split(input.subject, ".")) == 3
  input.action == "subscribe"
}

reason := "allow: checkpoint subject" if {
  startswith(input.subject, "swarm.checkpoint.")
  count(split(input.subject, ".")) == 3
}

deny_reason contains msg if {
  contains(input.subject, "*")
  msg := "deny: wildcard subjects are not allowed"
}

deny_reason contains msg if {
  contains(input.subject, ">")
  msg := "deny: wildcard subjects are not allowed"
}

deny_reason contains msg if {
  startswith(input.subject, "swarm.rpc.response.")
  expected := concat("", ["swarm.rpc.response.", input.gateway_id])
  input.subject != expected
  msg := "deny: rpc response subject must be self-scoped"
}

deny_reason contains msg if {
  startswith(input.subject, "swarm.advisory.suicide.")
  expected := concat("", ["swarm.advisory.suicide.", input.gateway_id])
  input.subject != expected
  msg := "deny: suicide advisory subject must be self-scoped"
}

deny_reason contains msg if {
  input.subject == "swarm.discovery"
  input.action == "publish"
  object.get(input.payload, "gateway_id", "") != input.gateway_id
  msg := "deny: discovery publish must announce self"
}

reason := msg if {
  some msg in deny_reason
}
