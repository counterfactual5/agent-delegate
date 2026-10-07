"""
OpenClaw RuntimeAdapter 实现

将 agent-delegate 的抽象接口映射到 OpenClaw 的 sessions_spawn / sessions_yield API。
"""

import subprocess
import os

from src.models.base import RuntimeAdapter, SpawnResult, WorkerOutput


class OpenClawAdapter(RuntimeAdapter):
    """OpenClaw runtime 适配器"""

    def __init__(self, agent_id: str = None, session_prefix: str = None):
        self.agent_id = agent_id
        self.session_prefix = session_prefix
        self._openclaw_bin = os.environ.get("OPENCLAW_BIN", "openclaw")
        self._completed_runs = {}  # Cache results from blocking spawn calls

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
            if result.returncode == 0:
                # Parse output: last line is run_id, everything else is the result
                lines = result.stdout.strip().split("\n") if result.stdout else []
                run_id = lines[-1] if lines else "unknown"
                output_summary = "\n".join(lines[:-1]) if len(lines) > 1 else "completed"
                
                # Cache the result for listen() to retrieve
                self._completed_runs[run_id] = WorkerOutput(
                    success=True,
                    summary=output_summary,
                )
                return SpawnResult(run_id=run_id, status="completed")
            else:
                error_msg = result.stderr[:500] if result.stderr else "Unknown error"
                return SpawnResult(run_id="", status="error", error=error_msg)
        except subprocess.TimeoutExpired:
            return SpawnResult(run_id="", status="error", error="Timeout")
        except Exception as e:
            return SpawnResult(run_id="", status="error", error=str(e))

    def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
        """OpenClaw 模式下，spawn 本身是阻塞的，结果已缓存"""
        if run_id in self._completed_runs:
            return self._completed_runs[run_id]
        else:
            # Fallback for unknown run_id
            return WorkerOutput(
                success=False,
                summary=f"Run {run_id} not found in completed cache",
            )

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
