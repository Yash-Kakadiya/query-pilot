"""Grounded answer synthesis and conservative deterministic presentation service (Task 1.28).

Provides:
- Deterministic empty-result messaging.
- Conservative semantic-aware scalar templates.
- Isolated multi-row LLM narrative generation strictly marked UNVERIFIED_NARRATIVE.
- Failsafe fallback preserving tabular/scalar presentation if synthesis is unavailable or fails.
- Zero database access, zero SQL generation, and zero SQL execution capabilities.
"""

from enum import Enum
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from query_pilot.db.executor import QueryResult
from query_pilot.observability import LLMUsageMetadata, extract_llm_usage

logger = logging.getLogger(__name__)


# ==============================================================================
# Grounded Answer Status & Models
# ==============================================================================

class GroundedAnswerStatus(str, Enum):
    """Classification of the answer source and factual confidence."""

    TEMPLATE_DETERMINISTIC = "TEMPLATE_DETERMINISTIC"
    UNVERIFIED_NARRATIVE = "UNVERIFIED_NARRATIVE"
    EMPTY_RESULT = "EMPTY_RESULT"
    SYNTHESIS_FAILED = "SYNTHESIS_FAILED"
    SKIPPED = "SKIPPED"
    UNAVAILABLE = "UNAVAILABLE"


class GroundedAnswer(BaseModel):
    """Structured natural-language answer model attached to PresentationResult."""

    text: str = Field(..., description="The user-facing natural language answer text.")
    status: GroundedAnswerStatus = Field(..., description="Classification of the answer confidence.")
    is_mechanically_verified: bool = Field(
        default=False,
        description="True strictly for deterministic templates; False for all LLM prose.",
    )
    verification_notes: List[str] = Field(
        default_factory=list,
        description="Caveats, truncation disclosures, or verification notes.",
    )
    usage_metadata: Optional[LLMUsageMetadata] = Field(
        default=None,
        description="Provider token usage and latency if an LLM call was performed.",
    )

    @property
    def markdown_text(self) -> str:
        """Alias for text for markdown presentation."""
        return self.text

    @property
    def narrative(self) -> Optional[str]:
        """Narrative prose if answer is an unverified LLM narrative, else None."""
        if self.status == GroundedAnswerStatus.UNVERIFIED_NARRATIVE:
            return self.text
        return None


# ==============================================================================
# Conservative Deterministic Scalar Template Formatter
# ==============================================================================

def _extract_column_tokens(name: str) -> List[str]:
    """Tokenize a column name by snake_case, kebab-case, or CamelCase word boundaries."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    tokens = re.split(r"[_\-\s]+", s)
    return [t.lower() for t in tokens if t]


def format_scalar_template(col_name: str, val: Any) -> str:
    """Format a single-row, single-column result using conservative column semantics.

    Rules:
    - Never infer a count merely from the 1x1 result shape or the presence of 'total' or 'num'.
    - Unambiguous count columns must contain 'count' or 'cnt' tokens, without competing indicators.
    - Unambiguous average columns must contain 'avg', 'average', or 'mean' tokens.
    - Unambiguous minimum columns must contain 'min', 'minimum', or 'lowest' tokens.
    - Unambiguous maximum columns must contain 'max', 'maximum', or 'highest' tokens.
    - If ambiguous, conflicting, or non-aggregate names are present, format neutrally as '{ColumnName}: {val}'.
    - Missing or unexpected values format safely as 'None' or native string.

    Returns:
        Formatted template string.
    """
    col_clean = str(col_name).strip()
    val_str = "None" if val is None else str(val)
    tokens = _extract_column_tokens(col_clean)
    token_set = set(tokens)

    # Competing / non-count terms that must prevent count classification
    non_count_terms = {
        "revenue", "sales", "profit", "marks", "score", "fee", "price",
        "cost", "amount", "rate", "pct", "percent", "percentage",
        "discount", "number", "id", "code",
    }

    has_avg = bool(token_set & {"avg", "average", "mean"})
    has_min = bool(token_set & {"min", "minimum", "lowest"})
    has_max = bool(token_set & {"max", "maximum", "highest"})
    has_count = bool(token_set & {"count", "cnt"})

    # Check for competing aggregate labels (e.g. min AND max, or avg AND min)
    matched_agg_count = sum([has_avg, has_min, has_max, has_count])
    if matched_agg_count > 1:
        # Ambiguous competing aggregate tokens -> neutral fallback
        return f"{col_clean}: {val_str}"

    # 1. Unambiguous Average
    if has_avg:
        return f"Average: {val_str}"

    # 2. Unambiguous Minimum
    if has_min:
        return f"Minimum: {val_str}"

    # 3. Unambiguous Maximum
    if has_max:
        return f"Maximum: {val_str}"

    # 4. Unambiguous Count (must have count/cnt token and NO non-count indicator)
    if has_count and not (token_set & non_count_terms):
        return f"Count: {val_str}"

    # 5. Neutral fallback for arbitrary scalar projections or ambiguous totals
    return f"{col_clean}: {val_str}"


# ==============================================================================
# Shared Answer Synthesis Service
# ==============================================================================

SYNTHESIS_SYSTEM_INSTRUCTION = """You are QueryPilot's result synthesis engine. Your task is to provide a concise, factual summary (1-2 sentences) directly answering the user question using ONLY the provided query result data.

CRITICAL CONSTRAINTS:
1. Grounding in Data: State only facts directly observable in the sample rows. Never fabricate, extrapolate, speculate, or introduce external knowledge.
2. No Causal Claims: Do not use causal language (e.g. "because", "due to", "led to", "caused by"). Report what the data shows, not why it happened.
3. Truncation Awareness: If results are marked as truncated, you must explicitly state that the answer describes only the retrieved sample and that overall totals cannot be inferred.
4. Output Style: Be concise, direct, and professional. Do not refer to database tables, SQL queries, or internal column names.
"""


class AnswerSynthesisService:
    """Shared service for deterministic and synthesized answer formatting."""

    def __init__(
        self,
        client: Optional[Any] = None,
        gemini_client: Optional[Any] = None,
        model_name: str = "gemini-2.0-flash",
        enabled: bool = True,
        max_sample_rows: int = 15,
    ):
        """Initialize synthesis service.

        Args:
            client: Optional google-genai Client instance.
            gemini_client: Alias for client for compatibility.
            model_name: Configured model identifier.
            enabled: If False or client is None, multi-row synthesis falls back cleanly.
            max_sample_rows: Maximum sample rows supplied in synthesis prompt (bounds payload).
        """
        self.client = gemini_client if gemini_client is not None else client
        self.model_name = model_name
        self.enabled = enabled
        self.max_sample_rows = max_sample_rows

    def _init_gemini_client(self) -> Optional[Any]:
        """Lazily initialize Google GenAI client if not provided."""
        if self.client is not None:
            return self.client
        try:
            from google import genai

            return genai.Client()
        except Exception:
            return None

    def synthesize(
        self,
        question: str,
        query_result: QueryResult,
        request_id: Optional[str] = None,
    ) -> GroundedAnswer:
        """Produce a grounded natural-language answer for an executed QueryResult.

        Guarantees:
        - Never throws exceptions; falls back cleanly on any failure.
        - Zero database access and zero SQL execution.
        - Deterministic templates for 0-row and scalar results.
        - Multi-row LLM output is strictly marked UNVERIFIED_NARRATIVE.

        Args:
            question: The user natural-language question.
            query_result: The executed read-only QueryResult.
            request_id: Optional correlation ID for telemetry.

        Returns:
            GroundedAnswer model.
        """
        # Tier 0: Empty results (deterministic, zero LLM calls)
        if query_result.row_count == 0:
            return GroundedAnswer(
                text="Query executed successfully. No rows returned.",
                status=GroundedAnswerStatus.EMPTY_RESULT,
                is_mechanically_verified=True,
                verification_notes=[],
            )

        # Tier 1: Scalar results (1 row and 1 column, deterministic template)
        if len(query_result.columns) == 1 and query_result.row_count == 1:
            col = query_result.columns[0]
            val = query_result.rows[0].get(col) if query_result.rows else None
            text = format_scalar_template(col, val)
            is_agg = text.startswith(("Count:", "Average:", "Minimum:", "Maximum:"))
            notes = []
            if not is_agg:
                notes.append("Scalar column does not match standard aggregate naming; formatted neutrally.")
            return GroundedAnswer(
                text=text,
                status=GroundedAnswerStatus.TEMPLATE_DETERMINISTIC,
                is_mechanically_verified=True,
                verification_notes=notes,
            )

        # Tier 2: Multi-row / Multi-col results
        if not self.enabled:
            return GroundedAnswer(
                text="Tabular query results retrieved successfully.",
                status=GroundedAnswerStatus.SKIPPED,
                is_mechanically_verified=False,
                verification_notes=["LLM synthesis disabled; refer to result table."],
            )

        client = self._init_gemini_client()
        if client is None:
            return GroundedAnswer(
                text="Tabular query results retrieved successfully.",
                status=GroundedAnswerStatus.UNAVAILABLE,
                is_mechanically_verified=False,
                verification_notes=["LLM synthesis client not configured; refer to result table."],
            )

        # Build bounded synthesis prompt (zero SQL, zero database schema)
        sample = query_result.rows[: self.max_sample_rows]
        prompt_parts = [
            f"User Question: {question}",
            f"Columns: {', '.join(query_result.columns)}",
            f"Total Returned Rows: {query_result.row_count}",
        ]
        if query_result.truncated:
            prompt_parts.append(
                f"Truncation Notice: Result set was truncated at the system limit. Only the first {len(sample)} rows are provided."
            )
        prompt_parts.append("\nSample Data Rows:")
        for idx, r in enumerate(sample, 1):
            prompt_parts.append(f"Row {idx}: {r}")

        prompt_text = "\n".join(prompt_parts)

        start_time = time.perf_counter()
        response = None
        try:
            from google.genai import types

            config = types.GenerateContentConfig(
                system_instruction=SYNTHESIS_SYSTEM_INSTRUCTION,
                temperature=0.0,
            )
            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt_text,
                config=config,
            )
        except Exception as exc:
            logger.warning("Answer synthesis provider call failed.")
            return GroundedAnswer(
                text="Query executed successfully. Refer to the data table for results.",
                status=GroundedAnswerStatus.SYNTHESIS_FAILED,
                is_mechanically_verified=False,
                verification_notes=["Answer synthesis provider call failed."],
            )
        finally:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        usage_meta = extract_llm_usage(
            response=response,
            stage="answer_synthesis",
            model_name=self.model_name,
            latency_ms=elapsed_ms,
        )

        raw_text = getattr(response, "text", None)
        if not raw_text or not raw_text.strip():
            logger.warning("Answer synthesis returned empty text.")
            return GroundedAnswer(
                text="Query executed successfully. Refer to the data table for results.",
                status=GroundedAnswerStatus.SYNTHESIS_FAILED,
                is_mechanically_verified=False,
                verification_notes=["Synthesis returned empty text response."],
                usage_metadata=usage_meta,
            )

        notes = []
        if query_result.truncated:
            notes.append("Results were truncated at the system limit; narrative reflects only retrieved sample.")

        return GroundedAnswer(
            text=raw_text.strip(),
            status=GroundedAnswerStatus.UNVERIFIED_NARRATIVE,
            is_mechanically_verified=False,
            verification_notes=notes,
            usage_metadata=usage_meta,
        )
