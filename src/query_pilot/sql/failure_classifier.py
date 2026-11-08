"""Structured generation failure classifier for QueryPilot Hosted API.

Classifies downstream model provider exceptions into safe, stable taxonomies:
- quota_exhausted: Provider rate limit or free-tier quota ceiling reached.
- authentication_failed: Invalid, expired, or missing provider API key or permissions.
- provider_unavailable: 5xx, high capacity, overloaded, or service degradation.
- network_error: Transport, socket, DNS, or gateway connectivity interruptions.
- unknown: Any unmapped or internal model generation failure.

Security Invariants:
- Never exposes API keys, raw provider payloads, internal tracebacks, or stack traces.
- Preserves request IDs for telemetry and diagnostics.
- Only reports retry guidance when supported by reliable provider metadata (e.g. explicit retry delays).
- Never invents or falsely estimates quota reset times.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from query_pilot.api.models import (
    GenerationFailureClassification,
    GenerationFailureDiagnostic,
)

logger = logging.getLogger(__name__)

# Regex pattern for extracting reliable retry delays (e.g., 'retryDelay': '12070s' or 'retry in 42s' or Retry-After)
RETRY_DELAY_PATTERN = re.compile(
    r"(?:retrydelay['\":\s]+|retry_after['\":\s]+|retry in\s+)(\d+(?:\.\d+)?)\s*s?",
    re.IGNORECASE,
)


def extract_retry_guidance(raw_text: str) -> Optional[str]:
    """Extract safe, human-readable retry guidance from reliable provider metadata.

    Returns None if no unambiguous retry delay is present. Does not invent quota reset times.
    """
    if not raw_text:
        return None

    match = RETRY_DELAY_PATTERN.search(raw_text)
    if not match:
        return None

    try:
        seconds = float(match.group(1))
        if seconds <= 0:
            return None
        if seconds < 60:
            secs_int = int(seconds)
            return f"Provider requested waiting at least {secs_int} second{'s' if secs_int != 1 else ''} before retrying."
        elif seconds < 3600:
            mins = max(1, round(seconds / 60))
            return f"Provider requested waiting at least {mins} minute{'s' if mins != 1 else ''} before retrying."
        else:
            hours = round(seconds / 3600, 1)
            h_str = f"{int(hours)}" if hours.is_integer() else f"{hours}"
            return f"Provider requested waiting at least {h_str} hour{'s' if hours != 1.0 else ''} before retrying."
    except Exception:
        return None


def classify_generation_failure(exc: Exception) -> GenerationFailureDiagnostic:
    """Classify an exception from the model provider into safe structured diagnostics."""
    # Gather inspection strings across the exception chain
    parts = [type(exc).__name__, str(exc)]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(type(cause).__name__)
        parts.append(str(cause))

    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code is None and cause is not None:
        status_code = getattr(cause, "status_code", None) or getattr(cause, "code", None)

    combined_text = " ".join(parts).lower()

    # 1. Quota Exhaustion / Rate Limiting (429 / RESOURCE_EXHAUSTED)
    if (
        status_code in (429, "429", "RESOURCE_EXHAUSTED")
        or "429" in combined_text
        or "resource_exhausted" in combined_text
        or "quota exceeded" in combined_text
        or "rate limit" in combined_text
        or "too many requests" in combined_text
        or "generaterequestsper" in combined_text
    ):
        retry_guidance = extract_retry_guidance(str(exc))
        if not retry_guidance and cause is not None:
            retry_guidance = extract_retry_guidance(str(cause))

        return GenerationFailureDiagnostic(
            classification=GenerationFailureClassification.QUOTA_EXHAUSTED,
            message="Model provider request limit or quota exhausted. Please try again later or check your API quota.",
            retry_guidance=retry_guidance,
        )

    # 2. Authentication / Authorization Failure (401 / 403 / API_KEY_INVALID)
    if (
        status_code in (401, 403, "401", "403", "UNAUTHENTICATED", "PERMISSION_DENIED")
        or "unauthenticated" in combined_text
        or "permission_denied" in combined_text
        or "api_key_invalid" in combined_text
        or "invalid api key" in combined_text
        or "unauthorized" in combined_text
        or "forbidden" in combined_text
    ):
        return GenerationFailureDiagnostic(
            classification=GenerationFailureClassification.AUTHENTICATION_FAILED,
            message="Model provider authentication failed. Please verify provider credentials and access permissions.",
            retry_guidance=None,
        )

    # 3. Provider Service Unavailable / Overloaded (503 / 502 / 504 / 500)
    if (
        status_code in (500, 502, 503, 504, "500", "502", "503", "504", "UNAVAILABLE")
        or "unavailable" in combined_text
        or "high demand" in combined_text
        or "service unavailable" in combined_text
        or "overloaded" in combined_text
        or "bad gateway" in combined_text
        or "gateway timeout" in combined_text
    ):
        retry_guidance = extract_retry_guidance(str(exc))
        if not retry_guidance and cause is not None:
            retry_guidance = extract_retry_guidance(str(cause))
        if not retry_guidance:
            retry_guidance = "Service is experiencing high demand. Please retry in a few moments."

        return GenerationFailureDiagnostic(
            classification=GenerationFailureClassification.PROVIDER_UNAVAILABLE,
            message="Model provider service is temporarily unavailable or experiencing high demand. Please try again shortly.",
            retry_guidance=retry_guidance,
        )

    # 4. Network / Connectivity Error
    if (
        "connecterror" in combined_text
        or "connecttimeout" in combined_text
        or "networkerror" in combined_text
        or "timeoutexception" in combined_text
        or "timeouterror" in combined_text
        or "timed out" in combined_text
        or "timeout" in combined_text
        or "connection" in combined_text
        or "connection refused" in combined_text
        or "dns resolution" in combined_text
        or "socket" in combined_text
        or "remotedisconnected" in combined_text
    ):
        return GenerationFailureDiagnostic(
            classification=GenerationFailureClassification.NETWORK_ERROR,
            message="Network connectivity error communicating with model provider.",
            retry_guidance="Check your network connection and proxy configuration before retrying.",
        )

    # 5. Unknown / Fallback
    return GenerationFailureDiagnostic(
        classification=GenerationFailureClassification.UNKNOWN,
        message="An unexpected error occurred during candidate SQL generation.",
        retry_guidance=None,
    )
