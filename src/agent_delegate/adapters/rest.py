"""
Generic REST API RuntimeAdapter

适用于任何提供 REST API 的 LLM runtime。
"""

import http.client
import json
import logging
import time
import urllib.request
import urllib.error
from typing import Optional

from agent_delegate.models.base import RuntimeAdapter, SpawnResult, WorkerOutput

logger = logging.getLogger(__name__)


#: listen() 的默认轮询间隔（秒）。
DEFAULT_POLL_INTERVAL = 2.0


def _request_error(resp) -> Optional[str]:
    """
    判断一次 _request() 的结果是否属于失败形态，是则返回错误文本，否则返回 None。

    _request() 把 HTTP / 网络错误统一编码成不带 status 的 {"error": "..."}。
    带 status 的响应来自远端本身，由调用方按 status 判断；远端常见的
    {"status": "completed", "error": null} 不是错误。spawn() 和 listen() 共用这一判定。
    """
    if not isinstance(resp, dict) or "status" in resp or not resp.get("error"):
        return None
    return str(resp["error"])


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
        "poll_interval": 2.0,
    }
    """

    #: 远端明确表示「还在进行中」的状态。status 不在此集合内说明响应形状不符合预期，
    #: 属于错误，应立即返回而不是空转到 deadline。
    IN_PROGRESS_STATUSES = frozenset({"pending", "queued", "running", "in_progress"})

    def __init__(self, config: dict, poll_interval: Optional[float] = None):
        self.base_url = config["base_url"].rstrip("/")
        self.headers = config.get("headers", {})
        self.spawn_endpoint = config.get("spawn_endpoint", "/agents/spawn")
        self.listen_endpoint = config.get("listen_endpoint", "/agents/{run_id}/status")
        self.send_endpoint = config.get("send_endpoint", "/messages/send")
        # 优先级：显式参数 > config > 默认值。测试可设为 0 免去真实 sleep。
        self.poll_interval = float(
            poll_interval if poll_interval is not None
            else config.get("poll_interval", DEFAULT_POLL_INTERVAL)
        )

    def _request(self, method: str, path: str, data: dict = None, timeout: int = 60) -> dict:
        """发一次 HTTP 请求。

        网络/协议类异常（URLError、超时、OS 层、JSON 解析、http.client
        协议错误）统一编码成 {"error": ...}；编程错误（TypeError 等）向外抛。
        """
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Content-Type": "application/json",
            **self.headers,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError, http.client.HTTPException) as e:
            return {"error": str(e) or type(e).__name__}

    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        resp = self._request("POST", self.spawn_endpoint, {
            "task": task,
            "model": model,
            "thinking": kwargs.get("thinking", "off"),
            "timeout_seconds": kwargs.get("timeout_seconds", 300),
            "cleanup": kwargs.get("cleanup", False),
        }, timeout=kwargs.get("timeout_seconds", 300))
        error = _request_error(resp)
        if error is not None:
            return SpawnResult(run_id="", status="error", error=error)
        # 远端可能用 failed/rejected 等非 "error" 的 status 表示失败，同时带 error 文本
        if resp.get("error") and resp.get("status") not in self.IN_PROGRESS_STATUSES | {"completed"}:
            return SpawnResult(run_id="", status="error", error=resp["error"])
        return SpawnResult(
            run_id=resp.get("run_id", "unknown"),
            status=resp.get("status", "pending"),
        )

    def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
        """
        轮询 run 的状态，直到完成、失败或超时。

        只有远端明确说「还在进行中」时才继续轮询；传输失败（{"error": ...}）或响应
        形状不符预期会立即返回失败，不会占用整个 timeout。
        """
        deadline = time.time() + timeout_ms / 1000
        path = self.listen_endpoint.format(run_id=run_id)
        # 至少查询一次；睡到 deadline 后再查最后一次，避免恰好在最后一轮完成的 run 被判超时。
        while True:
            outcome = self._interpret_response(self._request("GET", path), run_id)
            if outcome is not None:
                return outcome
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(self.poll_interval, remaining))
        # 远端只是还没给终态：标为未完成而不是失败，调用方不应据此重派。
        return WorkerOutput(success=False, summary="Timeout waiting for agent", incomplete=True)

    def _interpret_response(self, resp, run_id: str) -> Optional[WorkerOutput]:
        """
        把一次状态响应翻译成 WorkerOutput。

        返回 None 表示「远端明确说还在进行中」，调用方应继续轮询；
        其它一切情况（完成、失败、形状不符预期）都立即翻译成终态。
        """
        # 1. _request 的失败形态：传输 / HTTP 错误，必须立刻暴露原始错误文本。
        error = _request_error(resp)
        if error is not None:
            return WorkerOutput(success=False, summary=error)

        # 2. 非 dict 响应：状态接口不该返回数组或标量，形状不对就是错误。
        if not isinstance(resp, dict):
            return WorkerOutput(
                success=False,
                summary=f"Unexpected listen response for run {run_id}: {resp!r}",
            )

        status = resp.get("status")

        # 3. 正常终态：完成。
        if status == "completed":
            return WorkerOutput(
                success=True,
                summary=resp.get("summary") or f"Run {run_id} completed",
                output_path=resp.get("output_path"),
                artifacts=resp.get("artifacts") or [],
                issues=resp.get("issues") or [],
            )

        # 4. 正常终态：远端报告错误。
        if status == "error":
            return WorkerOutput(
                success=False,
                summary=resp.get("error") or "Unknown error",
                issues=resp.get("issues") or [],
            )

        # 5. 进行中：继续轮询。
        if status in self.IN_PROGRESS_STATUSES:
            return None

        # 6. status 缺失或取值未知：不是「还在跑」，是响应坏了，别拿它耗完 timeout。
        return WorkerOutput(
            success=False,
            summary=f"Unexpected listen status {status!r} for run {run_id}",
        )

    def send(self, message: str, **kwargs) -> None:
        """尽力通知：失败记 warning，不向调用方抛异常。"""
        resp = self._request("POST", self.send_endpoint, {
            "message": message,
            "channel": kwargs.get("channel", "default"),
            "to": kwargs.get("to"),
        })
        error = _request_error(resp)
        if error is not None:
            logger.warning("send() failed: %s", error)

    def list_runs(self, **kwargs) -> list:
        resp = self._request("GET", "/agents/runs")
        return resp if isinstance(resp, list) else []
