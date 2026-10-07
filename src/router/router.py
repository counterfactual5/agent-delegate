"""
Router - execution engine with model fallback.

The caller decides WHAT to run and on WHICH model (via task.task_type
mapped to chains, or task.model_override for direct selection).
The Router handles HOW: retries, provider isolation, degradation,
and audit trail.
"""

from src.models.base import (
    Task, TaskType, FallbackChain, ModelCandidate, DEFAULT_CHAINS, SpawnResult,
    RuntimeAdapter, ErrorClass, classify_error, AttemptRecord,
)


class Router:
    """Execution engine: dispatch with fallback, retry, and audit."""

    def __init__(self, adapter: RuntimeAdapter, chains: dict = None):
        self.adapter = adapter
        self.chains = chains or DEFAULT_CHAINS

    def select_model(self, task_type: TaskType) -> FallbackChain:
        """Return the candidate chain for a task type (config lookup only)."""
        if task_type in self.chains:
            return self.chains[task_type]
        if TaskType.STANDARD in self.chains:
            return self.chains[TaskType.STANDARD]
        values = list(self.chains.values())
        if not values:
            return FallbackChain(candidates=[])
        return values[0]

    @staticmethod
    def pack_context(context: str, task_desc: str, constraints: list[str] = None) -> str:
        """
        Pack context with XML tags to isolate data from instructions
        (prompt injection protection).
        """
        parts = [f"<context>\n{context}\n</context>\n"]
        parts.append(f"<task>\n{task_desc}\n</task>\n")
        if constraints:
            parts.append("<constraints>\n")
            for c in constraints:
                parts.append(f"- {c}\n")
            parts.append("</constraints>\n")
        return "".join(parts)

    def dispatch_with_fallback(self, task: Task, context: str = None, wait: bool = True) -> SpawnResult:
        """
        Dispatch with automatic model fallback.

        Decision comes from the caller:
        - task.model_override: use this specific model (no chain lookup)
        - task.task_type: look up chain from self.chains (config-driven)

        Execution is the Router's job:
        - 429/auth: blacklist entire provider, skip to next provider
        - 5xx: retry same model once, then degrade
        - timeout: prefer faster candidates
        - context overflow: prefer larger context_window models
        - adapter exceptions (runtime): convert to failure, continue fallback
        - adapter exceptions (programming): re-raise

        Returns SpawnResult with structured AttemptRecord audit trail.
        """
        if task.model_override:
            chain = FallbackChain(candidates=[
                ModelCandidate(
                    model_id=task.model_override,
                    provider=task.model_override.split("/")[0] if "/" in task.model_override else "custom",
                    speed_rank=1,
                )
            ])
        else:
            chain = self.select_model(task.task_type)

        packed = self.pack_context(
            context=context or "(no additional context)",
            task_desc=task.description,
        )

        attempts: list[AttemptRecord] = []
        dead_providers: set[str] = set()
        retried_server: set[str] = set()

        queue = list(chain.candidates)
        while queue:
            candidate = queue.pop(0)
            if candidate.provider in dead_providers:
                attempts.append(AttemptRecord(
                    model=candidate.model_id,
                    provider=candidate.provider,
                    outcome="skip",
                    reason=f"provider {candidate.provider} blacklisted",
                ))
                continue

            try:
                result = self.adapter.spawn(
                    task=packed,
                    model=candidate.model_id,
                    timeout_seconds=task.timeout_seconds,
                    wait=wait,
                )
            except (ConnectionError, TimeoutError, RuntimeError, ValueError, OSError) as e:
                result = SpawnResult(
                    run_id="",
                    status="error",
                    error=f"adapter raised: {type(e).__name__}: {e}",
                )
            except Exception:
                raise

            if result.status == "completed":
                result.model = candidate.model_id
                attempts.append(AttemptRecord(
                    model=candidate.model_id,
                    provider=candidate.provider,
                    outcome="ok",
                    status=result.status,
                ))
                result.attempts = attempts
                return result

            err_class = classify_error(result)
            attempts.append(AttemptRecord(
                model=candidate.model_id,
                provider=candidate.provider,
                outcome="fail",
                status=result.status,
                error_class=err_class.value,
                error=result.error,
            ))

            if err_class in (ErrorClass.RATE_LIMIT, ErrorClass.AUTH):
                dead_providers.add(candidate.provider)
            elif err_class == ErrorClass.SERVER_ERROR and candidate.model_id not in retried_server:
                retried_server.add(candidate.model_id)
                queue.insert(0, candidate)
            elif err_class == ErrorClass.TIMEOUT:
                queue.sort(key=lambda c: c.speed_rank)
            elif err_class == ErrorClass.CONTEXT_LENGTH:
                failed_window = candidate.context_window
                queue = [c for c in queue if c.context_window > failed_window]
                queue.sort(key=lambda c: (-c.context_window, c.speed_rank))

        return SpawnResult(
            run_id="", status="error",
            error="all candidates exhausted", attempts=attempts,
        )
