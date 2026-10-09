"""CLI script to generate an evaluation reliability report from saved results (Task 1.17).

Usage:
    python scripts/generate_evaluation_report.py --input data/evaluation/baseline_results_1_14.json --output data/evaluation/reliability_report_1_14.md

Validates the input artifact, parses records, calculates deterministic metrics,
and writes an evidence-based Markdown report without making Gemini API calls
or querying the database.
"""

import argparse
from pathlib import Path
import sys

# Ensure src is on sys.path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from query_pilot.eval.report import generate_markdown_report, load_evaluation_artifact

PROTECTED_FILES = frozenset({
    "baseline_results.json",
    "baseline_report.md",
    "baseline_results_1_14.json",
    "baseline_report_1_14.md",
    "baseline_questions.json",
})


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Generate an evidence-based Markdown reliability report from saved QueryPilot evaluation results.",
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        type=Path,
        help="Path to the saved evaluation JSON artifact (e.g. data/evaluation/baseline_results_1_14.json).",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Path to write the generated Markdown report. If omitted, defaults to <input_stem>_reliability_report.md.",
    )
    parser.add_argument(
        "--title", "-t",
        type=str,
        default=None,
        help="Optional custom title for the generated report.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print report to standard output instead of writing to a file.",
    )
    return parser.parse_args(args)


def main(args=None) -> int:
    parsed = parse_args(args)

    input_path = parsed.input.resolve()

    # 1. Validate input file exists
    if not input_path.exists():
        print(f"Error: Input file does not exist: {input_path}", file=sys.stderr)
        return 1
    if not input_path.is_file():
        print(f"Error: Input path is not a file: {input_path}", file=sys.stderr)
        return 1

    # 2. Determine and validate output path
    if parsed.output is not None:
        output_path = parsed.output.resolve()
    else:
        output_filename = f"{input_path.stem}_reliability_report.md"
        output_path = input_path.parent / output_filename

    # Guard: prevent accidental overwriting of protected historical artifacts
    if output_path.name in PROTECTED_FILES:
        print(
            f"Error: Target output path '{output_path.name}' is a protected historical artifact and cannot be overwritten. "
            f"Please specify a different output path using --output.",
            file=sys.stderr,
        )
        return 1

    # 3. Load and validate evaluation artifact
    try:
        report_data = load_evaluation_artifact(input_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Validation Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error loading evaluation artifact: {exc}", file=sys.stderr)
        return 1

    # 4. Generate deterministic Markdown report
    try:
        report_md = generate_markdown_report(report_data=report_data, title=parsed.title)
    except Exception as exc:
        print(f"Error generating markdown report: {exc}", file=sys.stderr)
        return 1

    # 5. Output report
    if parsed.stdout:
        print(report_md)
        return 0

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"Successfully generated evaluation report: {output_path}")
        return 0
    except Exception as exc:
        print(f"Error writing report to {output_path}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
