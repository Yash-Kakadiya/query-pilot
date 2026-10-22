"""Grounded answer synthesis and conservative deterministic presentation service.

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
from query_pilot.tracing import trace_gemini_call

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


class AnswerGroundingStatus(str, Enum):
    """Classification of metadata-only answer grounding outcome."""

    GROUNDED_FROM_METADATA = "grounded_from_metadata"
    REQUIRES_ROW_DISCLOSURE = "requires_row_disclosure"
    UNAVAILABLE = "unavailable"


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

METADATA_SYNTHESIS_SYSTEM_INSTRUCTION = """You are QueryPilot's metadata-only result synthesis engine.
Your task is to provide a concise, factual summary (1-2 sentences) describing the query execution outcome using ONLY the provided query metadata.

CRITICAL PRIVACY & GROUNDING CONSTRAINTS:
1. Grounding in Metadata Only: You have access ONLY to query metadata (user question, executed SQL, column names, row count, execution time, and truncation status). You DO NOT have access to actual row records or cell values.
2. Zero Hallucination: NEVER invent, fabricate, guess, or extrapolate specific data values, names, IDs, dates, or dollar amounts.
3. Row-Disclosure Awareness: If answering the user's question requires knowing specific individual cell contents or entity values (which are not present in metadata), you must clearly state that the query executed successfully and returned the specified number of rows, and direct the user to view the local data table for specific values.
4. No Causal Claims: Report what the metadata shows. Do not use causal or speculative language.
5. Truncation Awareness: If results were truncated at the system limit, explicitly mention that the count reflects the retrieved sample.
"""


def _question_requires_row_disclosure(question: str) -> bool:
    """Determine conservatively whether a question asks for specific cell values or entities.

    If a question asks for specific entity names, details, or values rather than
    high-level execution metrics, answer synthesis from metadata alone cannot ground the specific answer.
    """
    q = question.lower().strip()

    patterns = [
        r"\bwho\b",
        r"\bwhich\s+(?:student|employee|department|course|user|customer|order|item|product|faculty|instructor|teacher|person|record|row)\b",
        r"\bwhat\s+(?:is|are|was|were)\s+the\s+(?:name|email|phone|address|salary|gpa|score|grade|price|cost|amount|total|sum|average|balance|status)\b",
        r"\b(?:highest|lowest|maximum|minimum|max|min|top|best|worst|cheapest|most expensive)\b",
        r"\b(?:list|show|give|find|display|get)\s+(?:all\s+)?(?:the\s+)?(?:names|emails|details|salaries|records|students|employees|courses)\b",
        r"\b(?:total\s+revenue|total\s+sales|sum\s+of|average\s+gpa|average\s+salary)\b",
    ]
    for pat in patterns:
        if re.search(pat, q):
            return True
    return False


def mask_sql_literals(sql: str, dialect: str = "tsql") -> Optional[str]:
    """Mask all literal constants in candidate SQL AST using sqlglot.

    Replaces string and numeric constants throughout the query AST with generic
    placeholders ('[REDACTED_VALUE]') to prevent sensitive values (e.g. SSNs,
    emails, phone numbers, salary amounts) from leaking into synthesis prompts.
    Preserves SQL relational structure (tables, columns, joins, operators).

    Returns:
        Transformed SQL string, or None if AST parsing fails (fail-closed).
    """
    if not sql or not isinstance(sql, str) or not sql.strip():
        return None

    try:
        import sqlglot
        from sqlglot import exp

        ast = sqlglot.parse_one(sql.strip(), read=dialect)
        if not ast:
            return None

        def _mask_node(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Literal):
                if node.is_string:
                    return exp.Literal.string("[REDACTED_VALUE]")
                elif node.is_number:
                    return exp.Literal.string("[REDACTED_NUMBER]")
                return exp.Literal.string("[REDACTED_LITERAL]")
            return node

        masked_ast = ast.transform(_mask_node)
        return masked_ast.sql(dialect=dialect)
    except Exception as exc:
        logger.warning("Failed to parse SQL AST for literal masking [%s]: %s", type(exc).__name__, exc)
        return None


def _detect_hallucinated_values(
    text: str,
    question: str,
    candidate_sql: str,
    columns: List[str],
    row_count: int,
    execution_time_ms: float,
) -> bool:
    """Detect if the LLM output attempted to fabricate specific values or entity names.

    Heuristic defense-in-depth:
    Inspects model output for currency symbols or unexpected numeric constants not grounded
    in the execution metadata.
    NOTE: This is a bounded heuristic and not an exhaustive semantic fact-checker. It cannot
    guarantee detection of non-numeric textual hallucinations, dates, or fabricated qualitative claims.
    """
    if any(sym in text for sym in ("$", "€", "£", "₹", "¥")):
        return True

    numbers_in_text = re.findall(r"\b\d+(?:\.\d+)?\b", text)
    allowed_numbers = {
        str(row_count),
        str(len(columns)),
        str(int(execution_time_ms)),
        f"{execution_time_ms:.1f}",
        f"{execution_time_ms:.0f}",
    }
    for n in re.findall(r"\b\d+(?:\.\d+)?\b", question + " " + candidate_sql):
        allowed_numbers.add(n)

    for num_str in numbers_in_text:
        if num_str not in allowed_numbers:
            return True

    return False


class AnswerSynthesisService:
    """Shared service for deterministic and synthesized answer formatting."""

    def __init__(
        self,
        client: Optional[Any] = None,
        gemini_client: Optional[Any] = None,
        model_name: str = "gemini-3.8-flash",
        enabled: bool = True,
        max_sample_rows: int = 15,
        http_options: Optional[Any] = None,
    ):
        """Initialize synthesis service.

        Args:
            client: Optional google-genai Client instance.
            gemini_client: Alias for client for compatibility.
            model_name: Configured model identifier.
            enabled: If False or client is None, multi-row synthesis falls back cleanly.
            max_sample_rows: Maximum sample rows supplied in synthesis prompt (bounds payload).
            http_options: Optional custom types.HttpOptions (defaults to bounded retry settings).
        """
        self.client = gemini_client if gemini_client is not None else client
        self.model_name = model_name
        self.enabled = enabled
        self.max_sample_rows = max_sample_rows
        self.http_options = http_options

    def _init_gemini_client(self) -> Optional[Any]:
        """Lazily initialize Google GenAI client if not provided."""
        if self.client is not None:
            return self.client
        try:
            from google import genai
            from query_pilot.sql.gemini_generator import get_gemini_http_options

            opts = self.http_options if self.http_options is not None else get_gemini_http_options()
            return genai.Client(http_options=opts)
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
            from query_pilot.sql.gemini_generator import should_include_sampling_parameters

            config_kwargs: dict = {
                "system_instruction": SYNTHESIS_SYSTEM_INSTRUCTION,
            }
            if should_include_sampling_parameters(self.model_name):
                config_kwargs["temperature"] = 0.0

            config = types.GenerateContentConfig(**config_kwargs)
            response = self._call_gemini_model(
                client=client,
                model_name=self.model_name,
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

    @trace_gemini_call(name="gemini_answer_synthesis", run_type="llm")
    def _call_gemini_model(
        self, client: Any, model_name: str, contents: str, config: Any
    ) -> Any:
        """Isolated helper for invoking the Gemini models API under tracing."""
        return client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )

    def synthesize_from_metadata(
        self,
        question: str,
        candidate_sql: str,
        columns: List[str],
        row_count: int,
        truncated: bool = False,
        execution_time_ms: float = 0.0,
        request_id: Optional[str] = None,
    ) -> Tuple[str, AnswerGroundingStatus, Optional[str]]:
        """Synthesize a privacy-safe grounded answer using strictly approved query metadata.

        Guarantees:
        - Never receives, materializes, or transmits database row records or cell values.
        - Zero database connections or execution operations.
        - Returns AnswerGroundingStatus.GROUNDED_FROM_METADATA for empty or metadata-answerable results.
        - Returns AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE if the question or output requires undisclosed values.
        - Returns AnswerGroundingStatus.UNAVAILABLE if synthesis is disabled or fails.
        - Never throws unhandled exceptions; falls back safely to preserve local execution outcomes.

        Args:
            question: Original user natural-language question.
            candidate_sql: The validated read-only SQL query executed locally.
            columns: List of returned column names (names only, never cell values).
            row_count: Number of returned rows from local execution.
            truncated: Whether the local result set was truncated by the row limit.
            execution_time_ms: Local execution latency in milliseconds.
            request_id: Optional correlation tracking ID.

        Returns:
            Tuple of (summary_text, AnswerGroundingStatus, Optional[grounding_notes]).
        """
        # Tier 0: Empty results (deterministic, zero LLM calls)
        if row_count == 0:
            return (
                "Query executed successfully. No rows returned.",
                AnswerGroundingStatus.GROUNDED_FROM_METADATA,
                "Determined from metadata: zero rows returned.",
            )

        # Tier 1: Conservative pre-screening for questions requiring undisclosed row values
        if _question_requires_row_disclosure(question):
            return (
                f"Query executed successfully and returned {row_count} row{'s' if row_count != 1 else ''}. "
                "Because QueryPilot protects data privacy by not transmitting database records to the cloud AI, "
                "the specific values cannot be displayed in this narrative summary. Please view the result directly in the local data table below.",
                AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE,
                "Specific result values were not disclosed to the cloud AI under the zero-disclosure privacy policy.",
            )

        # Tier 2: Check synthesis enablement and client configuration
        if not self.enabled:
            return (
                f"Query executed successfully and returned {row_count} row{'s' if row_count != 1 else ''}. Refer to the local data table for results.",
                AnswerGroundingStatus.UNAVAILABLE,
                "Answer synthesis disabled; refer to result table.",
            )

        client = self._init_gemini_client()
        if client is None:
            return (
                f"Query executed successfully and returned {row_count} row{'s' if row_count != 1 else ''}. Summary is currently unavailable; please refer to the local data table.",
                AnswerGroundingStatus.UNAVAILABLE,
                "Answer synthesis client not configured; refer to result table.",
            )

        # Tier 3: Bounded metadata-only prompt (zero rows, zero cell values)
        masked_sql = mask_sql_literals(candidate_sql)
        if masked_sql:
            sql_prompt_entry = f"Executed SQL: {masked_sql}"
        else:
            # Fail-closed: omit raw SQL and use safe placeholder if parsing fails
            sql_prompt_entry = "Executed SQL: [STRUCTURE_UNAVAILABLE]"

        prompt_parts = [
            f"User Question: {question.strip()}",
            sql_prompt_entry,
            f"Columns: {', '.join(columns)}",
            f"Total Returned Rows: {row_count}",
            f"Execution Time: {execution_time_ms:.1f} ms",
        ]
        if truncated:
            prompt_parts.append(
                "Truncation Notice: Result set was truncated at the system limit. Only the first 100 rows were retrieved."
            )
        prompt_text = "\n".join(prompt_parts)

        response = None
        try:
            from google.genai import types
            from query_pilot.sql.gemini_generator import should_include_sampling_parameters

            config_kwargs: dict = {
                "system_instruction": METADATA_SYNTHESIS_SYSTEM_INSTRUCTION,
            }
            if should_include_sampling_parameters(self.model_name):
                config_kwargs["temperature"] = 0.0

            config = types.GenerateContentConfig(**config_kwargs)
            response = self._call_gemini_model(
                client=client,
                model_name=self.model_name,
                contents=prompt_text,
                config=config,
            )
        except Exception:
            logger.warning("Metadata answer synthesis provider call failed.")
            return (
                f"Query executed successfully and returned {row_count} row{'s' if row_count != 1 else ''}. Summary is currently unavailable; please refer to the local data table.",
                AnswerGroundingStatus.UNAVAILABLE,
                "Answer synthesis provider call failed.",
            )

        raw_text = getattr(response, "text", None)
        if not raw_text or not raw_text.strip():
            logger.warning("Metadata answer synthesis returned empty text.")
            return (
                f"Query executed successfully and returned {row_count} row{'s' if row_count != 1 else ''}. Summary is currently unavailable; please refer to the local data table.",
                AnswerGroundingStatus.UNAVAILABLE,
                "Synthesis returned empty text response.",
            )

        cleaned_text = raw_text.strip()
        # Tier 4: Post-screening to catch any hallucinated cell values or currency
        if _detect_hallucinated_values(
            cleaned_text,
            question=question,
            candidate_sql=candidate_sql,
            columns=columns,
            row_count=row_count,
            execution_time_ms=execution_time_ms,
        ):
            logger.info("Metadata answer synthesis detected hallucinated or undisclosed values; safely downgrading to requires_row_disclosure.")
            return (
                f"Query executed successfully and returned {row_count} row{'s' if row_count != 1 else ''}. "
                "Because QueryPilot protects data privacy by not transmitting database records to the cloud AI, "
                "the specific values cannot be displayed in this narrative summary. Please view the result directly in the local data table below.",
                AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE,
                "Specific result values were not disclosed to the cloud AI under the zero-disclosure privacy policy.",
            )

        note = "Results were truncated at the system limit; narrative reflects only retrieved count." if truncated else None
        return (cleaned_text, AnswerGroundingStatus.GROUNDED_FROM_METADATA, note)
