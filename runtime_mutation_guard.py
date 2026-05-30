import re


NO_MUTATION_RE = re.compile(
    r"\b(?:do not|don't|without)\s+(?:modify|change|write|delete|mutate)\b",
    re.IGNORECASE,
)
EXPLICIT_FILE_MUTATION_RE = re.compile(
    r"\b(?:write|create|edit|update|modify|delete|remove|append|save)\b"
    r"(?=.*\b(?:file|path|document|record|note|/proc|/tmp|/run|/uploads)\b)",
    re.IGNORECASE | re.DOTALL,
)


def raw_file_mutation_allowed(
    task_text: str,
    *,
    classified_intent: bool = False,
) -> bool:
    text = task_text.strip()
    if not text:
        return False
    if NO_MUTATION_RE.search(text):
        return False
    if classified_intent:
        return True
    if not EXPLICIT_FILE_MUTATION_RE.search(text):
        return False
    # Read-only analytical prompts can mention "record" or "file" because they
    # are evidence sources. Require a positive mutation verb to win over those
    # ordinary lookup words before exposing raw write/delete behavior.
    return True
