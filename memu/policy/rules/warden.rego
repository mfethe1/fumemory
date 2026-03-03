package memu.warden

default allow = false
default reason = "Denied by policy"

allow {
    input.action == "spawn"
    input.active_containers < input.max_containers
}

reason = "ok" {
    allow
}

reason = "budget exceeded" {
    input.action == "spawn"
    input.compute_budget >= 2000
}

