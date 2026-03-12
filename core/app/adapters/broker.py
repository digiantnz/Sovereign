import httpx

BROKER_URL = "http://docker-broker:8088"


class BrokerAdapter:
    """Proxies read and workflow operations to docker-broker via its HTTP API.

    Trust levels map directly to governance tiers:
      low    → read operations (containers list, logs, stats)
      medium → workflow operations (restart)
      high   → destructive operations (rolling-recreate, prune) — Phase 2
    """

    async def list_containers(self) -> list:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BROKER_URL}/containers/json",
                headers={"X-Trust-Level": "low"},
            )
            r.raise_for_status()
            return r.json()

    async def get_logs(self, container: str, tail: int = 50) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{BROKER_URL}/containers/{container}/logs",
                headers={"X-Trust-Level": "low"},
                params={"stdout": "1", "stderr": "1", "tail": str(tail)},
            )
            r.raise_for_status()
            return r.text

    async def get_stats(self, container: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BROKER_URL}/containers/{container}/stats",
                headers={"X-Trust-Level": "low"},
                params={"stream": "false"},
            )
            r.raise_for_status()
            return r.json()

    async def get_gpu_stats(self) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{BROKER_URL}/system/gpu",
                headers={"X-Trust-Level": "low"},
            )
            r.raise_for_status()
            return r.json()

    async def restart(self, container: str) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{BROKER_URL}/containers/{container}/restart",
                headers={"X-Trust-Level": "medium"},
            )
            r.raise_for_status()
            return {"status": "restarted", "container": container}

    # ── Read-only examination endpoints ──────────────────────────────────

    async def get_containers_full(self) -> dict:
        """Full docker ps -a with all fields including networks and mounts."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BROKER_URL}/system/containers",
                headers={"X-Trust-Level": "low"},
            )
            r.raise_for_status()
            return r.json()

    async def inspect_container(self, container: str) -> dict:
        """docker inspect for a named container."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BROKER_URL}/system/inspect/{container}",
                headers={"X-Trust-Level": "low"},
            )
            r.raise_for_status()
            return r.json()

    async def get_compose(self) -> dict:
        """Read current compose.yml from the host filesystem via broker."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BROKER_URL}/system/compose",
                headers={"X-Trust-Level": "low"},
            )
            r.raise_for_status()
            return r.json()

    async def read_host_file(self, path: str) -> dict:
        """Read a file or list a directory on the host filesystem (read-only)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{BROKER_URL}/fs/read",
                headers={"X-Trust-Level": "low"},
                params={"path": path},
            )
            r.raise_for_status()
            return r.json()

    async def get_hardware(self) -> dict:
        """Combined hardware info: GPU (nvidia-smi), disk (df), memory, CPU."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{BROKER_URL}/system/hardware",
                headers={"X-Trust-Level": "low"},
            )
            r.raise_for_status()
            return r.json()

    async def get_processes(self) -> dict:
        """ps aux — system process list."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{BROKER_URL}/system/processes",
                headers={"X-Trust-Level": "low"},
            )
            r.raise_for_status()
            return r.json()
