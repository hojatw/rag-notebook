"""Loopback-only event-loop liveness for the standalone ingest worker.

The CLI deliberately imports no worker/ingest/Chroma modules and never opens
SQLite or calls an LLM. This is liveness, not ingestion/dependency readiness.
"""
import asyncio
import math
import socket
import sys
import time

from .config import config

_HOST = "127.0.0.1"
_RESPONSE = b"notebooklm-worker-ok\n"


class _HealthProtocol(asyncio.Protocol):
    def connection_made(self, transport):
        # Executed on the same event loop as run_worker_loop. No background
        # thread can keep claiming health if that loop is blocked or stopped.
        transport.write(_RESPONSE)
        transport.close()


async def start_health_server(port: int) -> asyncio.Server:
    """Caller owns shutdown; binding errors fail worker startup."""
    return await asyncio.get_running_loop().create_server(_HealthProtocol, _HOST, port)


def probe(port: int, timeout: float) -> bool:
    """Require the exact worker response within one bounded time budget."""
    if not 0 < port < 65536 or not math.isfinite(timeout) or timeout <= 0:
        return False
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((_HOST, port), timeout=timeout) as connection:
            response = b""
            while len(response) < len(_RESPONSE):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                connection.settimeout(remaining)
                part = connection.recv(len(_RESPONSE) - len(response))
                if not part:
                    return False
                response += part
            return response == _RESPONSE
    except OSError:
        return False


def main() -> int:
    if probe(config.jobs.health_port, config.jobs.health_timeout_s):
        return 0
    print("worker liveness probe failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
