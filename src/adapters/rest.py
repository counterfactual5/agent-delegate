"""
Generic REST API RuntimeAdapter

适用于任何提供 REST API 的 LLM runtime。
"""

import json
import urllib.request
import urllib.error

from src.models.base import RuntimeAdapter, SpawnResult, WorkerOutput


class RESTAdapter(RuntimeAdapter):
    """
    通用 REST API 适配器。
    
    配置示例：
    {
        "base_url": "http://localhost:8080/api",
        "headers": {"Authorization": "Bearer xxx"},
        "spawn_endpoint": "/agents/spawn",
        "listen_endpoint": "/agents/{run_id}/status",
        "send_endpoint": "/messages/send",
    }
    """

    def __init__(self, config: dict):
        self.base_url = config["base_url"].rstrip("/")
        self.headers = config.get("headers", {})
        self.spawn_endpoint = config.get("spawn_endpoint", "/agents/spawn")
        self.listen_endpoint = config.get("listen_endpoint", "/agents/{run_id}/status")
        self.send_endpoint = config.get("send_endpoint", "/messages/send")

    def _request(self, method: str, path: str, data: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Content-Type": "application/json",
            **self.headers,
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        resp = self._request("POST", self.spawn_endpoint, {
            "task": task,
            "model": model,
            "thinking": kwargs.get("thinking", "off"),
            "timeout_seconds": kwargs.get("timeout_seconds", 300),
            "cleanup": kwargs.get("cleanup", False),
        })
        if "error" in resp:
            return SpawnResult(run_id="", status="error", error=resp["error"])
        return SpawnResult(
            run_id=resp.get("run_id", "unknown"),
            status=resp.get("status", "pending"),
        )

    def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
        import time
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            resp = self._request("GET", self.listen_endpoint.format(run_id=run_id))
            status = resp.get("status", "unknown")
            if status == "completed":
                return WorkerOutput(
                    success=True,
                    summary=resp.get("summary", ""),
                    output_path=resp.get("output_path"),
                    artifacts=resp.get("artifacts", []),
                )
            elif status == "error":
                return WorkerOutput(
                    success=False,
                    summary=resp.get("error", "Unknown error"),
                )
            time.sleep(2)
        return WorkerOutput(success=False, summary="Timeout waiting for agent")

    def send(self, message: str, **kwargs) -> None:
        self._request("POST", self.send_endpoint, {
            "message": message,
            "channel": kwargs.get("channel", "default"),
            "to": kwargs.get("to"),
        })

    def list_runs(self, **kwargs) -> list:
        resp = self._request("GET", "/agents/runs")
        return resp if isinstance(resp, list) else []
