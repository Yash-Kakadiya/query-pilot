"""Persistent and Heuristic Data Dictionary Store for QueryPilot Local Agent.

Manages durable plain-English data dictionaries stored in ~/.query_pilot/dictionaries/{profile_id}.json.
Provides:
- Thread-safe disk reads and writes via threading.RLock.
- Atomic updates via temporary file and os.replace.
- Deterministic offline heuristics translating technical database identifiers and acronyms into human business concepts.
- On-demand AI enrichment using Google Gemini Flash when configured, with seamless offline fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Dict, List, Optional

from query_pilot.agent.models import (
    ColumnDictionaryEntry,
    DataDictionaryResponse,
    TableDictionaryEntry,
)
from query_pilot.db.models import DatabaseSchema, TableMetadata

logger = logging.getLogger(__name__)

# Common database naming acronyms and abbreviation mappings to business terms
ACRONYM_MAP: Dict[str, str] = {
    "id": "Identifier",
    "fk": "Foreign Key",
    "pk": "Primary Key",
    "num": "Number",
    "no": "Number",
    "cd": "Code",
    "flg": "Flag",
    "flag": "Flag",
    "stat": "Status",
    "cst": "Customer",
    "cust": "Customer",
    "usr": "User",
    "user": "User",
    "emp": "Employee",
    "amt": "Amount",
    "dt": "Date",
    "date": "Date",
    "ts": "Timestamp",
    "qty": "Quantity",
    "desc": "Description",
    "addr": "Address",
    "tel": "Telephone",
    "ph": "Phone",
    "cat": "Category",
    "prd": "Product",
    "prod": "Product",
    "inv": "Invoice",
    "ord": "Order",
    "txn": "Transaction",
    "pct": "Percentage",
    "calc": "Calculation",
    "std": "Standard",
    "cnt": "Count",
    "val": "Value",
    "min": "Minimum",
    "max": "Maximum",
    "avg": "Average",
    "tot": "Total",
    "curr": "Currency",
    "bal": "Balance",
    "src": "Source",
    "tgt": "Target",
    "dst": "Destination",
    "ref": "Reference",
    "idx": "Index",
    "dept": "Department",
    "org": "Organization",
    "mgr": "Manager",
    "rate": "Rate",
    "fee": "Fee",
    "tax": "Tax",
    "cost": "Cost",
    "price": "Price",
    "rev": "Revenue",
    "net": "Net",
    "gross": "Gross",
    "actv": "Active",
    "del": "Deleted",
    "is": "Is",
    "has": "Has",
    "nm": "Name",
    "fname": "First Name",
    "lname": "Last Name",
    "mname": "Middle Name",
    "dob": "Date of Birth",
    "ssn": "Social Security Number",
    "req": "Required",
    "opt": "Optional",
    "sys": "System",
    "cfg": "Configuration",
}


def humanize_identifier(identifier: str) -> str:
    """Transform snake_case, camelCase, or PascalCase identifier into Title Cased phrase."""
    if not identifier:
        return ""
    # Split camelCase / PascalCase
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", identifier)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    tokens = re.split(r"[_\-\s]+", s2)
    expanded: List[str] = []
    for token in tokens:
        clean = token.strip()
        if not clean:
            continue
        lower = clean.lower()
        if lower in ACRONYM_MAP:
            expanded.append(ACRONYM_MAP[lower])
        else:
            expanded.append(clean.capitalize())
    return " ".join(expanded) if expanded else identifier


class DictionaryStore:
    """Thread-safe store for persistent plain-English database data dictionaries."""

    def __init__(self, dictionaries_dir: Path):
        self._dictionaries_dir = dictionaries_dir.resolve()
        self._lock = threading.RLock()
        try:
            self._dictionaries_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    @property
    def dictionaries_dir(self) -> Path:
        return self._dictionaries_dir

    def _get_path_for_profile(self, profile_id: str) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", profile_id)
        return self._dictionaries_dir / f"{safe_name}.json"

    def get_cached_count(self) -> int:
        """Count number of valid cached dictionary files."""
        with self._lock:
            if not self._dictionaries_dir.is_dir():
                return 0
            try:
                return len(list(self._dictionaries_dir.glob("*.json")))
            except Exception:
                return 0

    def get_dictionary(self, profile_id: str) -> Optional[DataDictionaryResponse]:
        """Load cached dictionary from disk for the given profile if available."""
        with self._lock:
            file_path = self._get_path_for_profile(profile_id)
            if not file_path.is_file():
                return None
            try:
                content = file_path.read_text(encoding="utf-8")
                if not content.strip():
                    return None
                data = json.loads(content)
                return DataDictionaryResponse(**data)
            except Exception as exc:
                logger.warning("Failed to load cached dictionary for '%s': %s", profile_id, exc)
                return None

    def save_dictionary(self, profile_id: str, dictionary: DataDictionaryResponse) -> None:
        """Save dictionary to disk atomically using temporary file and os.replace."""
        with self._lock:
            self._dictionaries_dir.mkdir(parents=True, exist_ok=True)
            target_path = self._get_path_for_profile(profile_id)
            temp_path = None
            try:
                payload = dictionary.model_dump()
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    dir=str(self._dictionaries_dir),
                    delete=False,
                    encoding="utf-8",
                    suffix=".tmp",
                ) as tmp:
                    temp_path = Path(tmp.name)
                    json.dump(payload, tmp, indent=2)
                    tmp.flush()
                    os.fsync(tmp.fileno())

                os.replace(str(temp_path), str(target_path))
                temp_path = None
            except Exception as exc:
                logger.error("Failed to save dictionary for '%s': %s", profile_id, exc)
            finally:
                if temp_path is not None and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass

    def generate_heuristic_dictionary(
        self,
        profile_id: str,
        database_name: str,
        schema: DatabaseSchema,
    ) -> DataDictionaryResponse:
        """Deterministically derive business names, explanations, and suggested questions from schema structure."""
        tables_dict: Dict[str, TableDictionaryEntry] = {}

        for table in schema.tables:
            human_table = humanize_identifier(table.table_name)
            pks = set(table.primary_keys)

            # Map FK source columns to target descriptions
            fk_map: Dict[str, str] = {}
            for fk in getattr(table, "foreign_keys", []) or []:
                if hasattr(fk, "source_column") and hasattr(fk, "target_table"):
                    tgt_col = getattr(fk, "target_column", "id")
                    fk_map[fk.source_column.lower()] = f"{fk.target_table}({tgt_col})"

            cols_dict: Dict[str, ColumnDictionaryEntry] = {}
            suggested_cols_for_breakdown: List[str] = []

            for col in table.columns:
                c_name = col.name
                c_lower = c_name.lower()
                human_col = humanize_identifier(c_name)
                is_pk = col.primary_key or c_name in pks or c_lower in {p.lower() for p in pks}
                is_fk = c_lower in fk_map
                fk_target = fk_map.get(c_lower)

                # Generate contextual description
                data_type_str = str(col.data_type).upper() if col.data_type else "TEXT"
                if is_pk:
                    desc = f"Primary unique identifier for records in {human_table}."
                elif is_fk:
                    desc = f"Foreign key relationship linking this record to {fk_target}."
                elif any(flag_word in c_lower for flag_word in ["flg", "flag", "is_", "has_", "actv"]):
                    desc = f"Boolean indicator status specifying whether this {human_table} record is {human_col.lower()}."
                elif any(dt_word in c_lower for dt_word in ["date", "dt", "ts", "time", "created", "updated"]):
                    desc = f"Timestamp recording when the {human_col.lower()} occurred for this record."
                elif any(amt_word in c_lower for amt_word in ["amt", "amount", "price", "cost", "total", "fee", "tax", "balance"]):
                    desc = f"Financial or numerical value recording the {human_col.lower()}."
                elif any(cnt_word in c_lower for cnt_word in ["count", "qty", "quantity", "num_"]):
                    desc = f"Quantity measurement indicating total {human_col.lower()}."
                else:
                    desc = f"Stores the {human_col.lower()} attribute for {human_table}."

                if not is_pk and not is_fk and "CHAR" in data_type_str and len(suggested_cols_for_breakdown) < 2:
                    suggested_cols_for_breakdown.append(c_name)

                cols_dict[c_name] = ColumnDictionaryEntry(
                    name=c_name,
                    business_name=human_col,
                    description=desc,
                    data_type=data_type_str,
                    is_primary_key=is_pk,
                    is_foreign_key=is_fk,
                    foreign_key_target=fk_target,
                )

            # Generate suggested questions
            questions: List[str] = [
                f"How many total records are in {table.table_name}?",
                f"What are the most recent 10 records in {table.table_name}?",
            ]
            if suggested_cols_for_breakdown:
                questions.append(
                    f"Show a breakdown of records in {table.table_name} grouped by {suggested_cols_for_breakdown[0]}."
                )

            table_desc = f"Contains records and relational attributes representing {human_table} within the {database_name} database."
            tables_dict[table.table_name] = TableDictionaryEntry(
                table_name=table.table_name,
                business_name=human_table,
                description=table_desc,
                suggested_questions=questions,
                columns=cols_dict,
            )

        now_str = datetime.now(timezone.utc).isoformat()
        return DataDictionaryResponse(
            profile_id=profile_id,
            database_name=database_name,
            source="deterministic_heuristic",
            generated_at=now_str,
            tables=tables_dict,
        )

    def generate_ai_enriched_dictionary(
        self,
        profile_id: str,
        database_name: str,
        schema: DatabaseSchema,
        gemini_api_key: Optional[str] = None,
    ) -> DataDictionaryResponse:
        """Attempt AI synthesis using Gemini Flash; falls back gracefully to heuristics if offline or without key."""
        # 1. First generate baseline heuristic dictionary
        baseline = self.generate_heuristic_dictionary(
            profile_id=profile_id,
            database_name=database_name,
            schema=schema,
        )

        effective_key = gemini_api_key or os.getenv("GEMINI_API_KEY")
        if not effective_key or not effective_key.strip():
            logger.info("No Gemini API key available; returning heuristic data dictionary.")
            self.save_dictionary(profile_id, baseline)
            return baseline

        # 2. Attempt LLM generation
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=effective_key.strip())

            # Prepare compact schema representation for LLM prompt
            schema_summary = []
            for t in schema.tables:
                col_defs = [f"{c.name} ({c.data_type})" for c in t.columns[:30]]
                schema_summary.append({
                    "table": t.table_name,
                    "columns": col_defs,
                    "primary_keys": list(t.primary_keys),
                    "foreign_keys": [
                        f"{fk.source_column} -> {fk.target_table}({fk.target_column})"
                        for fk in (getattr(t, "foreign_keys", []) or [])
                    ],
                })

            prompt = (
                "You are an expert enterprise database analyst. Given the following database schema context, "
                "generate a plain-English business data dictionary explaining the business meaning of each table "
                "and column, along with 2-3 practical analytical questions a business user might ask.\n\n"
                f"Database: {database_name}\n"
                f"Schema:\n{json.dumps(schema_summary, indent=2)}\n\n"
                "Respond with a JSON object conforming exactly to this schema:\n"
                "{\n"
                '  "tables": {\n'
                '    "<table_name>": {\n'
                '      "business_name": "Human-friendly business name",\n'
                '      "description": "Comprehensive explanation of what this table represents in business operations.",\n'
                '      "suggested_questions": ["Question 1", "Question 2"],\n'
                '      "columns": {\n'
                '        "<col_name>": {\n'
                '          "business_name": "Readable column label",\n'
                '          "description": "Plain English explanation of what this data field records."\n'
                "        }\n"
                "      }\n"
                "    }\n"
                "  }\n"
                "}"
            )

            # Gemini Flash configuration
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )

            raw_text = response.text if hasattr(response, "text") else ""
            if not raw_text or not raw_text.strip():
                logger.warning("Empty response from Gemini for data dictionary. Falling back to heuristic.")
                self.save_dictionary(profile_id, baseline)
                return baseline

            parsed = json.loads(raw_text)
            ai_tables = parsed.get("tables", {})

            # Merge AI enrichment onto baseline
            merged_tables: Dict[str, TableDictionaryEntry] = {}
            for t_name, base_t in baseline.tables.items():
                ai_t = ai_tables.get(t_name, {})
                t_biz = ai_t.get("business_name") or base_t.business_name
                t_desc = ai_t.get("description") or base_t.description
                t_questions = ai_t.get("suggested_questions") or base_t.suggested_questions
                ai_cols = ai_t.get("columns", {})

                merged_cols: Dict[str, ColumnDictionaryEntry] = {}
                for c_name, base_c in base_t.columns.items():
                    ai_c = ai_cols.get(c_name, {})
                    c_biz = ai_c.get("business_name") or base_c.business_name
                    c_desc = ai_c.get("description") or base_c.description
                    merged_cols[c_name] = ColumnDictionaryEntry(
                        name=c_name,
                        business_name=c_biz,
                        description=c_desc,
                        data_type=base_c.data_type,
                        is_primary_key=base_c.is_primary_key,
                        is_foreign_key=base_c.is_foreign_key,
                        foreign_key_target=base_c.foreign_key_target,
                    )

                merged_tables[t_name] = TableDictionaryEntry(
                    table_name=t_name,
                    business_name=t_biz,
                    description=t_desc,
                    suggested_questions=t_questions,
                    columns=merged_cols,
                )

            enriched = DataDictionaryResponse(
                profile_id=profile_id,
                database_name=database_name,
                source="ai_gemini_flash",
                generated_at=datetime.now(timezone.utc).isoformat(),
                tables=merged_tables,
            )
            self.save_dictionary(profile_id, enriched)
            return enriched

        except Exception as exc:
            logger.warning("AI dictionary enrichment failed (%s); returning baseline heuristic.", exc)
            self.save_dictionary(profile_id, baseline)
            return baseline
