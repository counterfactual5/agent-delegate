"""
Router - execution engine with model fallback.

The caller decides WHAT to run and on WHICH model (via task.task_type
mapped to chains, or task.model_override for direct selection).
The Router handles HOW: retries, provider isolation, degradation,
and audit trail.
"""

import logging
import time

from agent_delegate.models.base import (
    Task, TaskType, FallbackChain, ModelCandidate, DEFAULT_CHAINS, SpawnResult,
    RuntimeAdapter, ErrorClass, classify_error, AttemptRecord,
)

logger = logging.getLogger(__name__)


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
        Attempt outcome values: ok | fail | skip | incomplete (non-terminal
        status contract violation).
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
        last_non_terminal: tuple[str, str] | None = None  # (run_id, status)

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

            start_time = time.perf_counter()

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


            duration_ms = (time.perf_counter() - start_time) * 1000
            if result.status == "completed":
                result.model = candidate.model_id
                attempts.append(AttemptRecord(
                    model=candidate.model_id,
                    provider=candidate.provider,
                    outcome="ok",
                    status=result.status,
                    duration_ms=duration_ms,
                ))
                result.attempts = attempts
                return result

            # Handle non-terminal status (contract violation)
            if result.status not in ("completed", "error"):
                last_non_terminal = (result.run_id, result.status)
                logger.info(
                    "Non-terminal status for model=%s run_id=%s status=%s",
                    candidate.model_id, result.run_id, result.status
                )
                attempts.append(AttemptRecord(
                    model=candidate.model_id,
                    provider=candidate.provider,
                    outcome="incomplete",
                    status=result.status,
                    reason=f"non-terminal status: {result.status}",
                    duration_ms=duration_ms,
                ))
                continue

            err_class = classify_error(result)
            attempts.append(AttemptRecord(
                model=candidate.model_id,
                provider=candidate.provider,
                outcome="fail",
                status=result.status,
                error_class=err_class.value,
                error=result.error,
                duration_ms=duration_ms,
            ))

            if err_class in (ErrorClass.RATE_LIMIT, ErrorClass.AUTH):
                logger.warning(
                    "Blacklisting provider %s due to %s error",
                    candidate.provider, err_class.value
                )
                dead_providers.add(candidate.provider)
            elif err_class == ErrorClass.SERVER_ERROR and candidate.model_id not in retried_server:
                logger.info(
                    "Retrying model %s after SERVER_ERROR",
                    candidate.model_id
                )
                retried_server.add(candidate.model_id)
                queue.insert(0, candidate)
            elif err_class == ErrorClass.TIMEOUT:
                logger.debug("Re-sorting queue by speed_rank after TIMEOUT")
                queue.sort(key=lambda c: c.speed_rank)
            elif err_class == ErrorClass.CONTEXT_LENGTH:
                failed_window = candidate.context_window
                queue = [c for c in queue if c.context_window > failed_window]
                logger.debug(
                    "Filtered queue to context_window > %d, %d candidates remain",
                    failed_window, len(queue)
                )
                queue.sort(key=lambda c: (-c.context_window, c.speed_rank))

        # Compact aggregate summary; the full per-attempt trail stays in `attempts`.
        fail_classes: dict[str, int] = {}
        for a in attempts:
            if a.outcome == "fail" and a.error_class:
                fail_classes[a.error_class] = fail_classes.get(a.error_class, 0) + 1
        attempted = sum(1 for a in attempts if a.outcome != "skip")
        base = (f"all candidates exhausted (attempted={attempted}, "
                f"blacklisted={sorted(dead_providers)}, error_classes={fail_classes})")
        if last_non_terminal:
            run_id, status = last_non_terminal
            error = f"{base} (last non-terminal run_id={run_id}, status={status})"
        else:
            run_id = ""
            error = base
        
        logger.warning("All candidates exhausted: %s", error)
        return SpawnResult(
            run_id=run_id, status="error",
            error=error, attempts=attempts,
        )
