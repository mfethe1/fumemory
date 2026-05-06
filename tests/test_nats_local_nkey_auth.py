"""Tests for local NATS NKey authentication wiring (issue #35).

These are unit tests — no live NATS server required.  They verify that:
- NATS_LOCAL_NKEY_SEED is applied only to the LOCAL cluster node.
- NATS_LOCAL_NKEY_SEED takes priority over NATS_NKEY_SEED for LOCAL.
- NATS_NKEY_SEED falls back correctly when NATS_LOCAL_NKEY_SEED is absent.
- NATS_CREDS_FILE falls back correctly when neither NKey seed is set.
- The Railway node never picks up NATS_LOCAL_NKEY_SEED.
- resolve_local_nkey_seed() in nats_config returns the env value.
- The NATS server conf file declares an authorization block.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from memu.cluster import ClusterNode, NATSClusterManager
from memu.nats_config import resolve_local_nkey_seed

CONF_PATH = Path(__file__).parents[1] / "infra" / "local-nats" / "nats-server.conf"

LOCAL_SEED = "SUABC123LOCALSEEDNKEYXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
GLOBAL_SEED = "SUGLOBAL456NKEYXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
CREDS_FILE = "/tmp/test.creds"


@pytest.fixture(autouse=True)
def _clear_nats_env(monkeypatch):
    """Remove NATS auth env vars before each test for isolation."""
    for key in ("NATS_LOCAL_NKEY_SEED", "NATS_NKEY_SEED", "NATS_CREDS_FILE", "NATS_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def _manager() -> NATSClusterManager:
    return NATSClusterManager(
        local_url="nats://localhost:4222",
        railway_url="nats://railway:4222",
    )


# --- NATS_LOCAL_NKEY_SEED isolation ----------------------------------------

def test_local_nkey_seed_applied_to_local_node(monkeypatch):
    monkeypatch.setenv("NATS_LOCAL_NKEY_SEED", LOCAL_SEED)
    opts = _manager()._build_auth_opts(ClusterNode.LOCAL)
    assert opts.get("nkeys_seed") == LOCAL_SEED


def test_local_nkey_seed_not_applied_to_railway_node(monkeypatch):
    monkeypatch.setenv("NATS_LOCAL_NKEY_SEED", LOCAL_SEED)
    opts = _manager()._build_auth_opts(ClusterNode.RAILWAY)
    assert "nkeys_seed" not in opts
    assert "user_credentials" not in opts
    assert "token" not in opts


# --- Priority: NATS_LOCAL_NKEY_SEED > NATS_NKEY_SEED for LOCAL ---------------

def test_local_nkey_seed_takes_priority_over_global_for_local(monkeypatch):
    monkeypatch.setenv("NATS_LOCAL_NKEY_SEED", LOCAL_SEED)
    monkeypatch.setenv("NATS_NKEY_SEED", GLOBAL_SEED)
    opts = _manager()._build_auth_opts(ClusterNode.LOCAL)
    assert opts.get("nkeys_seed") == LOCAL_SEED


def test_global_nkey_seed_used_by_local_when_local_seed_absent(monkeypatch):
    monkeypatch.setenv("NATS_NKEY_SEED", GLOBAL_SEED)
    opts = _manager()._build_auth_opts(ClusterNode.LOCAL)
    assert opts.get("nkeys_seed") == GLOBAL_SEED


def test_global_nkey_seed_used_by_railway(monkeypatch):
    monkeypatch.setenv("NATS_NKEY_SEED", GLOBAL_SEED)
    opts = _manager()._build_auth_opts(ClusterNode.RAILWAY)
    assert opts.get("nkeys_seed") == GLOBAL_SEED


# --- Fallback: NATS_CREDS_FILE -----------------------------------------------

def test_creds_file_used_when_no_nkey_seed(monkeypatch):
    monkeypatch.setenv("NATS_CREDS_FILE", CREDS_FILE)
    for node in (ClusterNode.LOCAL, ClusterNode.RAILWAY):
        opts = _manager()._build_auth_opts(node)
        assert opts.get("user_credentials") == CREDS_FILE
        assert "nkeys_seed" not in opts


def test_local_nkey_seed_takes_priority_over_creds_file(monkeypatch):
    monkeypatch.setenv("NATS_LOCAL_NKEY_SEED", LOCAL_SEED)
    monkeypatch.setenv("NATS_CREDS_FILE", CREDS_FILE)
    opts = _manager()._build_auth_opts(ClusterNode.LOCAL)
    assert opts.get("nkeys_seed") == LOCAL_SEED
    assert "user_credentials" not in opts


# --- Token auth still works (existing behaviour) ----------------------------

def test_auth_token_takes_priority(monkeypatch):
    monkeypatch.setenv("NATS_LOCAL_NKEY_SEED", LOCAL_SEED)
    manager = NATSClusterManager(
        local_url="nats://localhost:4222",
        auth_token="tok-secret",
    )
    opts = manager._build_auth_opts(ClusterNode.LOCAL)
    assert opts.get("token") == "tok-secret"
    assert "nkeys_seed" not in opts


# --- Empty / whitespace env values treated as absent -------------------------

def test_empty_local_nkey_seed_falls_through_to_global(monkeypatch):
    monkeypatch.setenv("NATS_LOCAL_NKEY_SEED", "   ")
    monkeypatch.setenv("NATS_NKEY_SEED", GLOBAL_SEED)
    opts = _manager()._build_auth_opts(ClusterNode.LOCAL)
    assert opts.get("nkeys_seed") == GLOBAL_SEED


# --- nats_config.resolve_local_nkey_seed ------------------------------------

def test_resolve_local_nkey_seed_returns_value(monkeypatch):
    monkeypatch.setenv("NATS_LOCAL_NKEY_SEED", LOCAL_SEED)
    assert resolve_local_nkey_seed() == LOCAL_SEED


def test_resolve_local_nkey_seed_returns_none_when_absent(monkeypatch):
    monkeypatch.delenv("NATS_LOCAL_NKEY_SEED", raising=False)
    assert resolve_local_nkey_seed() is None


def test_resolve_local_nkey_seed_returns_none_for_empty_string(monkeypatch):
    monkeypatch.setenv("NATS_LOCAL_NKEY_SEED", "")
    assert resolve_local_nkey_seed() is None


# --- NATS server conf declares authorization block --------------------------

def test_nats_server_conf_has_authorization_block():
    conf = CONF_PATH.read_text()
    assert "authorization" in conf, "nats-server.conf must declare an authorization block"


def test_nats_server_conf_authorization_has_nkey_entry():
    conf = CONF_PATH.read_text()
    # Authorization block must contain at least one nkey entry
    assert re.search(r"nkey\s*:", conf), (
        "authorization block must include at least one nkey entry"
    )


def test_nats_server_conf_has_placeholder_or_real_public_key():
    conf = CONF_PATH.read_text()
    # Public key starts with 'U' followed by base32 chars (A-Z, 2-7)
    assert re.search(r'"U[A-Z2-7]{10,}"', conf), (
        "authorization.users[].nkey must contain a 'U'-prefixed public key"
    )
