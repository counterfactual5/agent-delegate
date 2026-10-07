"""
Agent Delegate - Production-grade multi-agent orchestration
"""

from .models.base import (
    Task, TaskType, DependencyType,
    ModelCandidate, FallbackChain, DEFAULT_CHAINS,
    SpawnResult, WorkerOutput, RuntimeAdapter,
    AttemptRecord, ErrorClass, load_chains
)
from .router.router import Router
from .workers.pipelines import PIPELINES, Pipeline, Stage
from .workers.runner import PipelineRunner, PipelineRun, StageRun
from .adapters.openclaw import OpenClawAdapter
from .adapters.rest import RESTAdapter

__version__ = "0.1.0"

__all__ = [
    "Router",
    "Task", "TaskType", "DependencyType",
    "ModelCandidate", "FallbackChain", "DEFAULT_CHAINS", "load_chains",
    "SpawnResult", "WorkerOutput", "RuntimeAdapter",
    "AttemptRecord", "ErrorClass",
    "Pipeline", "Stage", "PIPELINES", 
    "PipelineRunner", "PipelineRun", "StageRun",
    "OpenClawAdapter", "RESTAdapter",
]
