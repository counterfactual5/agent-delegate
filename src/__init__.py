"""
Agent Delegate - Production-grade multi-agent orchestration
"""

from .models.base import (
    Task, TaskType, ContextDependency, DependencyType,
    ModelCandidate, FallbackChain, DEFAULT_CHAINS,
    SpawnResult, WorkerOutput, RuntimeAdapter,
)
from .router.router import Router
from .workers.pipelines import PIPELINES, Pipeline, Stage
from .adapters.openclaw import OpenClawAdapter
from .adapters.rest import RESTAdapter

__version__ = "0.1.0"

__all__ = [
    "Router",
    "Task", "TaskType", "ContextDependency", "DependencyType",
    "ModelCandidate", "FallbackChain", "DEFAULT_CHAINS",
    "SpawnResult", "WorkerOutput", "RuntimeAdapter",
    "Pipeline", "Stage", "PIPELINES",
    "OpenClawAdapter", "RESTAdapter",
]
