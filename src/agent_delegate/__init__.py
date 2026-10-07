"""
Agent Delegate - Production-grade multi-agent orchestration
"""

from .models.base import (
    Task, TaskType, ContextDependency, DependencyType,
    ModelCandidate, FallbackChain, DEFAULT_CHAINS, ChainNotConfigured,
    SpawnResult, WorkerOutput, RuntimeAdapter,
    ErrorClass, classify_error,
)
from .router.router import Router
from .workers.pipelines import PIPELINES, Pipeline, Stage, StageStatus
from .workers.runner import PipelineRunner, PipelineResult, StageRecord
from .adapters.openclaw import OpenClawAdapter
from .adapters.rest import RESTAdapter

__version__ = "0.1.0"

__all__ = [
    "Router",
    "Task", "TaskType", "ContextDependency", "DependencyType",
    "ModelCandidate", "FallbackChain", "DEFAULT_CHAINS", "ChainNotConfigured",
    "SpawnResult", "WorkerOutput", "RuntimeAdapter",
    "ErrorClass", "classify_error",
    "Pipeline", "Stage", "StageStatus", "PIPELINES",
    "PipelineRunner", "PipelineResult", "StageRecord",
    "OpenClawAdapter", "RESTAdapter",
]
