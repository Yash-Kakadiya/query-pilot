"""QueryPilot evaluation and benchmarking module."""

from query_pilot.eval.evaluator import (
    compare_results,
    compute_evaluation_metrics,
    evaluate_case,
)
from query_pilot.eval.models import (
    CaseEvaluationResult,
    EvaluationMetrics,
    FailureStage,
    SemanticStatus,
)

__all__ = [
    "CaseEvaluationResult",
    "EvaluationMetrics",
    "FailureStage",
    "SemanticStatus",
    "evaluate_case",
    "compute_evaluation_metrics",
    "compare_results",
]
