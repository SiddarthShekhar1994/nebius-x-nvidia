"""Validate model output and retain only source-backed explicit deadline instants."""
import json
import re
from datetime import datetime, timezone

from keeper_core.adapters import InvalidProviderResponse
from keeper_core.contracts import TriageBucket

FIELDS = frozenset({
    "bucket", "reason", "career", "scam_signals", "deadline", "deadline_evidence", "confidence",
})
BUCKETS = frozenset(bucket.value for bucket in TriageBucket)
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")


def _invalid(message):
    raise InvalidProviderResponse("Invalid triage JSON: " + message)


def parse_payload(content: str) -> dict:
    if not isinstance(content, str) or len(content) > 32_000:
        _invalid("expected bounded text")
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        _invalid("expected JSON")
    if not isinstance(data, dict) or data.keys() != FIELDS:
        _invalid("fields do not match schema")
    bucket = data["bucket"]
    if bucket is not None and (not isinstance(bucket, str) or bucket not in BUCKETS):
        _invalid("unknown bucket")
    if not isinstance(data["reason"], str) or not 1 <= len(data["reason"].strip()) <= 500:
        _invalid("reason must be short nonempty text")
    if data["career"] is not None and type(data["career"]) is not bool:
        _invalid("career must be boolean or null")
    signals = data["scam_signals"]
    if isinstance(signals, str):
        # Nemotron Nano sometimes answers "a, b" instead of a JSON array.
        signals = [signal.strip() for signal in signals.split(",") if signal.strip()]
        data["scam_signals"] = signals
    if not isinstance(signals, list) or len(signals) > 20 or any(
        not isinstance(signal, str) or not 1 <= len(signal.strip()) <= 80 for signal in signals
    ):
        _invalid("invalid scam signals")
    evidence = data["deadline_evidence"]
    if evidence is not None and (not isinstance(evidence, str) or len(evidence) > 1000):
        _invalid("invalid deadline evidence")
    confidence = data["confidence"]
    if confidence is not None and (
        type(confidence) not in (int, float) or not 0 <= confidence <= 1
    ):
        _invalid("invalid confidence")
    return data


def verified_deadline(email, value, evidence):
    """Require a matching explicit instant in an exact span of source text."""
    if not isinstance(value, str) or not isinstance(evidence, str) or not evidence.strip():
        return None, None
    source = email.subject + "\n" + email.body_excerpt
    if evidence not in source or INSTANT.fullmatch(value) is None:
        return None, None
    try:
        candidate = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        instants = (
            datetime.fromisoformat(match.group().replace("Z", "+00:00")).astimezone(timezone.utc)
            for match in INSTANT.finditer(evidence)
        )
        if candidate in instants:
            return candidate, evidence
    except (ValueError, OverflowError):
        pass
    return None, None
