"""Prompt templates and prompt assembly for SQL generation."""

from typing import List
from query_pilot.sql.generation import SQLGenerationRequest, format_schema_context

SYSTEM_INSTRUCTION = """You are QueryPilot's SQL generation engine. Your task is to translate natural language questions into accurate, efficient, and strictly read-only SQL queries.

CRITICAL INSTRUCTIONS:
1. Target Dialect: You are generating SQL for Microsoft SQL Server (T-SQL). Use T-SQL syntax and built-in functions.
2. Read-Only Only: You may ONLY generate read-only query statements (SELECT, CTEs, JOINs, GROUP BY, aggregates). You must NEVER generate data-modifying or DDL statements (INSERT, UPDATE, DELETE, MERGE, DROP, ALTER, CREATE, TRUNCATE, EXEC, GRANT, etc.).
3. Grounding in Schema: You must use ONLY the tables and columns explicitly listed in the provided database schema context. Do NOT invent, assume, or hallucinate tables, columns, or relationships that are not defined.
4. No Sensitive Fields: Never query or refer to sensitive authentication fields (such as PasswordHash or secrets).
5. No Execution: Your role is purely to generate the candidate SQL query. The query will be deterministically validated by QueryPilot's safety validator before any execution.
6. Unanswerable Questions: If a question cannot be answered using the provided schema, provide a SELECT statement that returns an empty or explanatory result and explain the limitation in your explanation and assumptions.
7. Structured Output: You must return a structured JSON response containing:
   - "sql": The raw T-SQL query string (do NOT wrap in markdown code blocks).
   - "explanation": A concise explanation of how the query answers the user question.
   - "assumptions": A list of any specific assumptions made regarding filters, joins, or domain terms.
"""


def build_sql_generation_prompt(request: SQLGenerationRequest) -> str:
    """Construct the complete prompt for the LLM using the structured request.

    Formats the user's question, optional domain instructions, and the deterministic
    model-facing schema context into a clean prompt string.

    Args:
        request: The SQLGenerationRequest containing the question and schema context.

    Returns:
        Complete prompt string to be passed to the LLM.
    """
    schema_text = format_schema_context(request.schema_context, dialect=request.dialect)

    prompt_parts: List[str] = [
        "=== DATABASE SCHEMA CONTEXT ===",
        schema_text,
        "",
        "=== USER QUESTION ===",
        f"Question: {request.question}",
    ]

    if request.instructions and request.instructions.strip():
        prompt_parts.extend([
            "",
            "=== ADDITIONAL INSTRUCTIONS ===",
            request.instructions.strip(),
        ])

    prompt_parts.extend([
        "",
        "=== TASK ===",
        "Generate a valid T-SQL query to answer the user question based strictly on the schema above.",
    ])

    return "\n".join(prompt_parts)
