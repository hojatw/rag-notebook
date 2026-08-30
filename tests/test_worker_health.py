"""Worker liveness must exercise its event loop, not the web HTTP endpoint."""
import asyncio
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_compose_worker_has_its_own_probe():
    compose = (ROOT / "docker-compose.yml").read_text()
    worker = compose.split("\n  worker:\n", 1)[1]
    assert 'NOTEBOOKLM_JOBS_HEALTH_PORT: "8001"' in worker
    command = next(line.split("test: ", 1)[1] for line in worker.splitlines() if "test: " in line)
    assert json.loads(command) == ["CMD", "python", "-m", "app.worker_health"]
    assert "disable:" not in worker


def test_real_server_probe_and_shutdown():
    from app.worker_health import probe, start_health_server

    async def run():
        server = await start_health_server(0)
        async with server:
            address = server.sockets[0].getsockname()
            assert address[0] == "127.0.0.1"
            assert await asyncio.to_thread(probe, address[1], 1.0)
        assert not await asyncio.to_thread(probe, address[1], 0.1)

    asyncio.run(run())


def test_accepting_tcp_without_event_loop_response_is_not_healthy():
    from app.worker_health import probe

    # TCP connect can succeed via the kernel's backlog even if the process is
    # stopped or blocked. Only an application response proves responsiveness.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert not probe(listener.getsockname()[1], 0.1)


def test_unexpected_response_is_not_healthy():
    from app.worker_health import probe

    class WrongService(asyncio.Protocol):
        def connection_made(self, transport):
            transport.write(b"wrong-service\n")
            transport.close()

    async def run():
        server = await asyncio.get_running_loop().create_server(WrongService, "127.0.0.1", 0)
        async with server:
            assert not await asyncio.to_thread(probe, server.sockets[0].getsockname()[1], 0.5)

    asyncio.run(run())


def test_health_cli_disabled_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_CONFIG_FILE", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("NOTEBOOKLM_JOBS_HEALTH_PORT", "0")
    result = subprocess.run([sys.executable, "-m", "app.worker_health"], cwd=ROOT,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "worker liveness probe failed" in result.stderr


@pytest.mark.parametrize("crash", [False, True])
def test_standalone_worker_owns_health_server_lifetime(monkeypatch, crash):
    import app.worker as worker
    from app.worker_health import probe

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    monkeypatch.setattr(worker.config.jobs, "health_port", port)
    monkeypatch.setattr(worker, "init_db", lambda: None)

    async def fake_loop(**kwargs):
        # Same lifecycle as an ingest call waiting for provider I/O: a busy
        # worker must still answer liveness probes on the same event loop.
        assert await asyncio.to_thread(probe, port, 0.5)
        if crash:
            raise RuntimeError("worker loop failed")

    monkeypatch.setattr(worker, "run_worker_loop", fake_loop)
    if crash:
        with pytest.raises(RuntimeError, match="worker loop failed"):
            asyncio.run(worker._run_standalone())
    else:
        asyncio.run(worker._run_standalone())
    assert not probe(port, 0.1)


def test_health_settings_defaults_and_env(monkeypatch, tmp_path):
    from app.config import load_config

    monkeypatch.setenv("NOTEBOOKLM_CONFIG_FILE", str(tmp_path / "absent.toml"))
    assert load_config().jobs.health_port == 0
    assert load_config().jobs.health_timeout_s == 3.0
    monkeypatch.setenv("NOTEBOOKLM_JOBS_HEALTH_PORT", "8001")
    monkeypatch.setenv("NOTEBOOKLM_JOBS_HEALTH_TIMEOUT_S", "1.5")
    assert load_config().jobs.health_port == 8001
    assert load_config().jobs.health_timeout_s == 1.5
