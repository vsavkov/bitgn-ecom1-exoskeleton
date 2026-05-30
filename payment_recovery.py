import re
from collections.abc import Iterable, Sequence


ISO_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
PAYMENT_ID_RE = re.compile(r"\bpay_\d+\b")
PAYMENT_REF_RE = re.compile(r"/proc/payments/(pay_\d+)\.json\b")


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

