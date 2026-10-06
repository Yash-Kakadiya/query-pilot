"""Data models for deterministic SQL validation results and rejection reasons."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ValidationReasonCode(str, Enum):
    """Machine-readable validation decision and rejection codes."""

    EMPTY_SQL = "EMPTY_SQL"
    PARSE_ERROR = "PARSE_ERROR"
    MULTI_STATEMENT = "MULTI_STATEMENT"
    NOT_READ_ONLY = "NOT_READ_ONLY"
    FORBIDDEN_OPERATION = "FORBIDDEN_OPERATION"
    UNKNOWN_TABLE = "UNKNOWN_TABLE"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    SENSITIVE_COLUMN = "SENSITIVE_COLUMN"
    FORBIDDEN_TABLE = "FORBIDDEN_TABLE"
    EXCEEDS_MAX_ROWS = "EXCEEDS_MAX_ROWS"
    UNSUPPORTED_QUERY_SHAPE = "UNSUPPORTED_QUERY_SHAPE"


@dataclass(frozen=True)
class ValidationReason:
    """Individual reason for query validation rejection or warning."""

    code: ValidationReasonCode
    message: str
    location: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "code": self.code.value if isinstance(self.code, Enum) else str(self.code),
            "message": self.message,
            "location": self.location,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Structured decision returned by the deterministic SQL validator."""

    allowed: bool
    normalized_sql: Optional[str] = None
    reasons: List[ValidationReason] = field(default_factory=list)
    referenced_tables: List[str] = field(default_factory=list)
    referenced_columns: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def reason_codes(self) -> List[str]:
        """Convenience list of all failure reason codes."""
        return [r.code.value for r in self.reasons]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize validation result to a dictionary."""
        return {
            "allowed": self.allowed,
            "normalized_sql": self.normalized_sql,
            "reasons": [r.to_dict() for r in self.reasons],
            "referenced_tables": list(self.referenced_tables),
            "referenced_columns": list(self.referenced_columns),
            "warnings": list(self.warnings),
        }
