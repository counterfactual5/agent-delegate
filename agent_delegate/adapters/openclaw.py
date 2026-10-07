"""
OpenClaw RuntimeAdapter 实现

将 agent-delegate 的抽象接口映射到 OpenClaw 的 sessions_spawn / sessions_yield API。
"""

import logging
import subprocess
import os
from collections import OrderedDict

from agent_delegate.models.base import RuntimeAdapter, SpawnResult, WorkerOutput

_MAX_CACHED_RUNS = 100

logger = logging.getLogger(__name__)


class OpenClawAdapter(RuntimeAdapter):
    """OpenClaw runtime 适配器"""

    def __init__(self, agent_id: str = None, session_prefix: str = None):
        self.agent_id = agent_id
        self.session_prefix = session_prefix
        self._openclaw_bin = os.environ.get("OPENCLAW_BIN", "openclaw")
        self._completed_runs = OrderedDict()  # Cache results from blocking spawn calls

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
                
                # Cache the result for listen() to retrieve, avoiding sentinel/empty run_id
                if run_id and run_id != "unknown":
                    self._completed_runs[run_id] = WorkerOutput(
                        success=True,
                        summary=output_summary,
                    )
                    if len(self._completed_runs) > _MAX_CACHED_RUNS:
                        evicted_run_id, _ = self._completed_runs.popitem(last=False)
                        logger.debug(
                            "Evicted run_id=%s from cache (FIFO, size=%d)",
                            evicted_run_id, _MAX_CACHED_RUNS
                        )
                return SpawnResult(run_id=run_id, status="completed", summary=output_summary)
            else:
                error_msg = result.stderr[:500] if result.stderr else "Unknown error"
                return SpawnResult(run_id="", status="error", error=error_msg)
        except subprocess.TimeoutExpired:
            return SpawnResult(run_id="", status="error", error="Timeout")
        except (ConnectionError, TimeoutError, RuntimeError, ValueError, OSError) as e:
            return SpawnResult(run_id="", status="error", error=f"adapter raised: {type(e).__name__}: {e}")
        except Exception as e:
            # Programming errors: re-raise
            raise

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
        """通过 openclaw message send 发送消息（best-effort：失败只记日志，不抛异常）"""
        channel = kwargs.get("channel", "telegram")
        to = kwargs.get("to")
        cmd = [self._openclaw_bin, "message", "send", "--channel", channel, "-m", message]
        if to:
            cmd.extend(["-t", to])
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("send() failed for channel=%s: %s", channel, e)

    def list_runs(self, **kwargs) -> list:
        """列出活跃运行"""
        # OpenClaw 通过 subagents list API 实现
        return []
