import re
from collections.abc import Iterable, Sequence


ISO_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
PAYMENT_ID_RE = re.compile(r"\bpay_\d+\b")
PAYMENT_REF_RE = re.compile(r"/proc/payments/(pay_\d+)\.json\b")
# ECOM 3DS recovery treats an already-paid payment as a terminal unsupported
# state. Summaries can phrase the SQL status in several equivalent ways.
PAID_TERMINAL_STATUS_RE = re.compile(
    r"\balready\s+paid\b|"
    r"\bstatus\s*(?::|=|\bis\b)?\s*paid\b|"
    r"\bpayment_status\s*(?::|=|\bis\b)?\s*paid\b|"
    r"\bis\s+paid\b",
    flags=re.IGNORECASE,
)


def mentions_paid_terminal_state(text: str) -> bool:
    return bool(PAID_TERMINAL_STATUS_RE.search(text))


def payment_ids_from_refs_and_text(refs: Sequence[str], text: str) -> set[str]:
    payment_ids = set(PAYMENT_ID_RE.findall(text))
    for ref in refs:
        payment_ids.update(PAYMENT_REF_RE.findall(ref))
    return payment_ids


def retry_available_at_from_policy_text(
    content: str,
    *,
    payment_ids: Iterable[str],
) -> str:
    payment_ids = set(payment_ids)
    if "retry_available_at" not in content:
        return ""

    policy_payment_ids = set(PAYMENT_ID_RE.findall(content))
    if payment_ids and policy_payment_ids and payment_ids.isdisjoint(policy_payment_ids):
        return ""

    for line in content.splitlines():
        if "retry_available_at" not in line:
            continue
        if match := ISO_TIMESTAMP_RE.search(line):
            return match.group(0)
    return ""


def payment_recovery_message_with_retry_timestamp(
    message: str,
    *,
    retry_available_at: str,
) -> str:
    stripped = message.strip()
    if not retry_available_at or ISO_TIMESTAMP_RE.search(stripped):
        return message
    return (
        f"{stripped}: retry blocked until {retry_available_at}"
        if stripped
        else f"Retry blocked until {retry_available_at}"
    )


def payment_recovery_outcome_for_terminal_state(
    *,
    task_type: str,
    outcome: str,
    message: str,
    completed_steps_laconic: Sequence[str],
) -> str:
    if task_type != "payment_recovery":
        return outcome
    if outcome != "OUTCOME_NONE_CLARIFICATION":
        return outcome

    status_text = " ".join([message, *completed_steps_laconic])
    if mentions_paid_terminal_state(status_text):
        return "OUTCOME_NONE_UNSUPPORTED"
    return outcome
