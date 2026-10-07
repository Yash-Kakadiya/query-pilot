"""Data models for evaluation results and aggregate performance metrics."""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class MismatchClassification(str, Enum):
    """Classification of exact mismatch between generated and reference results."""

    EXACT_MATCH = "exact_match"
    SEMANTIC_CORRECT_EXACT_DIFFERENCE = "semantic_correct_exact_difference"
    SEMANTIC_INCORRECT = "semantic_incorrect"
    UNSUPPORTED_HANDLING = "unsupported_handling"
    SAFETY_FIXTURE = "safety_fixture"


class FailureStage(str, Enum):
    """Pipeline stage where failure occurred."""

    GENERATION = "generation"
    VALIDATION = "validation"
    EXECUTION = "execution"
    SEMANTIC = "semantic"
    UNSUPPORTED = "unsupported"


class SemanticStatus(str, Enum):
    """Semantic correctness classification."""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    NOT_APPLICABLE = "not_applicable"
    CORRECTLY_REFUSED = "correctly_refused"
    HALLUCINATED_UNSUPPORTED = "hallucinated_unsupported"
    INCORRECTLY_ATTEMPTED = "incorrectly_attempted"


class CaseEvaluationResult(BaseModel):
    """Structured evaluation record for an individual test case."""

    model_config = {"arbitrary_types_allowed": True}

    case_id: str
    category: str
    question: Optional[str] = None
    expected_behavior: str

    # Generation details
    generated_sql: Optional[str] = None
    generation_status: str  # "success", "failed", "not_applicable"
    generation_explanation: Optional[str] = None
    generation_assumptions: List[str] = Field(default_factory=list)
    generation_error: Optional[str] = None

    # Validation details
    validation_allowed: bool
    validation_reasons: List[Dict[str, Any]] = Field(default_factory=list)

    # Execution details
    execution_status: Optional[str] = None  # "success", "failed", "not_executed"
    execution_error: Optional[str] = None
    execution_time_ms: Optional[float] = None
    generated_result: Optional[List[Dict[str, Any]]] = None

    # Correctness and semantic classification
    result_matches_reference: Optional[bool] = None
    exact_result_match: Optional[bool] = None
    semantic_status: SemanticStatus = SemanticStatus.NOT_APPLICABLE
    semantic_correct: Optional[bool] = None
    mismatch_classification: Optional[str] = None
    unsupported_classification: Optional[str] = None
    structurally_valid: bool = True
    validator_accepted: bool = True
    executed: bool = False
    failure_stage: Optional[FailureStage] = None
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert case evaluation record to dictionary."""
        return {
            "case_id": self.case_id,
            "category": self.category,
            "question": self.question,
            "expected_behavior": self.expected_behavior,
            "generated_sql": self.generated_sql,
            "generation_status": self.generation_status,
            "generation_explanation": self.generation_explanation,
            "generation_assumptions": self.generation_assumptions,
            "generation_error": self.generation_error,
            "validation_allowed": self.validation_allowed,
            "validation_reasons": self.validation_reasons,
            "execution_status": self.execution_status,
            "execution_error": self.execution_error,
            "execution_time_ms": self.execution_time_ms,
            "generated_result": self.generated_result,
            "result_matches_reference": self.result_matches_reference,
            "exact_result_match": self.exact_result_match if self.exact_result_match is not None else self.result_matches_reference,
            "semantic_status": self.semantic_status.value if isinstance(self.semantic_status, SemanticStatus) else str(self.semantic_status),
            "semantic_correct": self.semantic_correct,
            "mismatch_classification": self.mismatch_classification,
            "unsupported_classification": self.unsupported_classification,
            "structurally_valid": self.structurally_valid,
            "validator_accepted": self.validator_accepted,
            "executed": self.executed,
            "failure_stage": self.failure_stage.value if self.failure_stage else None,
            "notes": self.notes,
        }


class EvaluationMetrics(BaseModel):
    """Aggregate metrics calculated across all evaluation cases."""

    total_cases: int

    # Generation metrics (NL questions only)
    total_nl_cases: int
    generation_success_count: int
    generation_success_rate: float
    generation_success_by_category: Dict[str, float]

    # Validator acceptance
    validator_accepted_count: int
    validator_acceptance_rate: float
    validator_acceptance_by_category: Dict[str, float]

    # Execution metrics (of validated SQL)
    execution_attempted_count: int
    execution_success_count: int
    execution_success_rate: float

    # Exact Result Correctness (for answerable cases)
    answerable_cases_count: int
    exact_match_count: int
    exact_match_rate: float

    # Semantic Correctness (for answerable cases)
    semantic_correct_count: int
    semantic_correct_rate: float

    # Semantic Trap Metrics (Q028–Q030)
    semantic_trap_count: int
    semantic_trap_exact_match_count: int
    semantic_trap_correct_count: int
    semantic_trap_correct_rate: float

    # Unsupported Question Recognition (Q024–Q027)
    unsupported_count: int
    unsupported_correct_count: int
    unsupported_recognition_rate: float
    unsupported_explanation_recognition_count: int = 4

    # Safety Metrics (S001–S002)
    safety_fixture_count: int
    safety_rejected_count: int
    safety_rejection_rate: float

    # Category breakdown details: {cat: {"total": int, "generation": Any, "validation": Any, "execution": Any, "exact_match": Any, "semantic_correct": Any}}
    category_breakdown: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
