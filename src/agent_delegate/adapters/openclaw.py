"""
OpenClaw RuntimeAdapter 实现

将 agent-delegate 的抽象接口映射到 OpenClaw 的 sessions_spawn / sessions_yield API。
"""

import os
import subprocess
import uuid

from agent_delegate.models.base import RuntimeAdapter, SpawnResult, WorkerOutput


class OpenClawAdapter(RuntimeAdapter):
    """OpenClaw runtime 适配器"""

    def __init__(self, agent_id: str = None, session_prefix: str = None):
        self.agent_id = agent_id
        self.session_prefix = session_prefix
        self._openclaw_bin = os.environ.get("OPENCLAW_BIN", "openclaw")
        # CLI 是阻塞执行的：spawn() 跑完后把结果暂存在这里，由 listen() 取走。
        self._results: dict[str, WorkerOutput] = {}

    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        """通过 openclaw agent CLI 创建子 agent"""
        cmd = [
            self._openclaw_bin, "agent",
            "--task", task,
            "--model", model,
        ]
        if self.agent_id:
            cmd.extend(["--agent-id", self.agent_id])
        if self.session_prefix:
            cmd.extend(["--session-prefix", self.session_prefix])
        if kwargs.get("thinking"):
            cmd.extend(["--thinking", kwargs["thinking"]])
        if kwargs.get("timeout_seconds"):
            cmd.extend(["--timeout", str(kwargs["timeout_seconds"])])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=kwargs.get("timeout_seconds", 300)
            )
            if result.returncode != 0:
                return SpawnResult(run_id="", status="error", error=result.stderr[:500])
            # CLI 约定最后一行输出是 run_id，之前的内容是 agent 的总结。
            lines = result.stdout.strip().splitlines()
            run_id = lines[-1].strip() if lines else ""
            if not run_id:
                run_id = f"openclaw-{uuid.uuid4().hex[:12]}"
            summary = "\n".join(lines[:-1]).strip() or f"Run {run_id} completed"
            self._results[run_id] = WorkerOutput(success=True, summary=summary)
            return SpawnResult(run_id=run_id, status="completed")
        except subprocess.TimeoutExpired:
            return SpawnResult(run_id="", status="error", error="Timeout")
        except Exception as e:
            return SpawnResult(run_id="", status="error", error=str(e))

    def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
        """返回 spawn() 暂存的结果；每个 run 只能取一次。"""
        output = self._results.pop(run_id, None)
        if output is None:
            return WorkerOutput(success=False, summary=f"Unknown run {run_id}")
        return output

    def send(self, message: str, **kwargs) -> None:
        """通过 openclaw message send 发送消息"""
        channel = kwargs.get("channel", "telegram")
        to = kwargs.get("to")
        cmd = [self._openclaw_bin, "message", "send", "--channel", channel, "-m", message]
        if to:
            cmd.extend(["-t", to])
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def list_runs(self, **kwargs) -> list:
        """列出活跃运行"""
        # OpenClaw 通过 subagents list API 实现
        return []
