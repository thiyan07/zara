"""Config-parsing / pure-logic tests for the runbook contract.

Covers scripts/runbook_lib.py ONLY: port picking from a given ss-output
string, pidfile read/validate, env redaction, DB layout. NO process
spawning, NO sockets, NO servers (per task constraints).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from runbook_lib import (  # noqa: E402
    cmdline_looks_ours,
    db_env_for_dir,
    logfile_path,
    parse_pidfile,
    parse_ss_listen_ports,
    pick_port,
    pidfile_path,
    redact_env,
)

SS_SAMPLE = """\
State  Recv-Q Send-Q Local Address:Port  Peer Address:Port
LISTEN 0      128    127.0.0.1:5432       0.0.0.0:*
LISTEN 0      128    127.0.0.1:8080       0.0.0.0:*
LISTEN 0      4096   [::1]:631             [::]:*
LISTEN 0      10     *:18080              *:*
garbage line without enough columns
LISTEN 0      10     127.0.0.1:notaport   0.0.0.0:*
"""


def test_parse_ss_listen_ports():
    ports = parse_ss_listen_ports(SS_SAMPLE)
    assert ports == {5432, 8080, 631, 18080}


def test_parse_ss_empty():
    assert parse_ss_listen_ports("") == set()
    assert parse_ss_listen_ports("State Recv-Q\n") == set()


def test_pick_explicit_free_port():
    port, fell_back = pick_port(19001, {8080})
    assert (port, fell_back) == (19001, False)


def test_pick_explicit_busy_port_refuses():
    # Caller (shell) must exit 2, never signal the occupant.
    port, fell_back = pick_port(8080, {8080})
    assert (port, fell_back) == (None, False)


def test_pick_default_free():
    port, fell_back = pick_port(None, {5432})
    assert (port, fell_back) == (8080, False)


def test_pick_default_busy_falls_back():
    occupied = {8080, 18080, 18081}
    port, fell_back = pick_port(None, occupied)
    assert (port, fell_back) == (18082, True)


def test_pick_range_exhausted():
    occupied = {8080} | set(range(18080, 18100))
    assert pick_port(None, occupied) == (None, False)


def test_pidfile_unique_per_port():
    assert pidfile_path(8080) != pidfile_path(18080)
    assert pidfile_path(8080).endswith("zara_local.8080.pid")
    assert logfile_path(8080).endswith("zara_local.8080.log")
    assert pidfile_path(8080).startswith("/tmp/opencode")


def test_parse_pidfile_valid():
    assert parse_pidfile("59631\n") == 59631
    assert parse_pidfile("  1234  ") == 1234


def test_parse_pidfile_stale_or_corrupt():
    assert parse_pidfile("") is None
    assert parse_pidfile("not-a-pid") is None
    assert parse_pidfile("0") is None
    assert parse_pidfile("-5") is None
    assert parse_pidfile("1") is None  # never signal PID 1


def test_cmdline_marker():
    assert cmdline_looks_ours("zara_local -m uvicorn core.app:app")
    assert cmdline_looks_ours("zara_local\x00-m\x00uvicorn\x00")
    assert not cmdline_looks_ours("python3 -m uvicorn core.app:app")
    assert not cmdline_looks_ours("")


def test_redact_env_hides_secrets_keeps_names():
    env = {
        "ASSISTANT_TOKEN": "super-secret",
        "LLM_API_KEY": "key-123",
        "NVIDIA_TTS_API_KEY": "nkey",
        "LLM_PROVIDER": "echo",
        "ZARA_MEMORY_DB": "/data/memory.db",
    }
    out = redact_env(env)
    assert set(out) == set(env)  # names never dropped
    assert out["ASSISTANT_TOKEN"] == "***"
    assert out["LLM_API_KEY"] == "***"
    assert out["NVIDIA_TTS_API_KEY"] == "***"
    assert out["LLM_PROVIDER"] == "echo"
    assert out["ZARA_MEMORY_DB"] == "/data/memory.db"
    for name, value in out.items():
        if "KEY" in name or "TOKEN" in name:
            assert "secret" not in value and "key-123" not in value


def test_redact_case_insensitive():
    out = redact_env({"assistant_token": "s3cr3t", "port": "8080"})
    assert out["assistant_token"] == "***"
    assert out["port"] == "8080"


def test_db_env_for_dir_layout():
    layout = db_env_for_dir("/tmp/opencode/zdata")
    assert layout == {
        "ZARA_MEMORY_DB": "/tmp/opencode/zdata/memory.db",
        "ZARA_MISSION_DB": "/tmp/opencode/zdata/missions.db",
        "ZARA_AUDIT_DB": "/tmp/opencode/zdata/audit.db",
        "ZARA_FABRIC_DB": "/tmp/opencode/zdata/fabric.db",
        "ZARA_SESSION_DB": "/tmp/opencode/zdata/session.db",
        "ZARA_CHAT_DB": "/tmp/opencode/zdata/chat.db",
    }


def test_db_env_empty_means_memory():
    assert db_env_for_dir("") == {}
