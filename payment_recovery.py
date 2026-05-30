import re
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath


ISO_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def payment_ids_from_refs_and_text(refs: Sequence[str], text: str) -> set[str]:
    payment_ids = _payment_ids_from_text(text)
    for ref in refs:
        path = ref.split("#", 1)[0]
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "proc" and parts[1] in {
            "payments",
            "payment-ledger",
        }:
            name = PurePosixPath(path).name.removesuffix(".json")
            if name.startswith(("pay-", "pay_")):
                payment_ids.add(name.lower())
    return payment_ids


def retry_available_at_from_policy_text(
    content: str,
    *,
    payment_ids: Iterable[str],
) -> str:
    payment_ids = set(payment_ids)
    if "retry_available_at" not in content:
        return ""

    policy_payment_ids = _payment_ids_from_text(content)
    if payment_ids and policy_payment_ids and payment_ids.isdisjoint(policy_payment_ids):
        return ""

    for line in content.splitlines():
        if "retry_available_at" not in line:
            continue
        if match := ISO_TIMESTAMP_RE.search(line):
            return match.group(0)
    return ""


def _payment_ids_from_text(text: str) -> set[str]:
    payment_ids: set[str] = set()
    chars = [char if char.isalnum() or char in {"-", "_"} else " " for char in text]
    for token in "".join(chars).split():
        lower = token.lower()
        if lower.startswith(("pay-", "pay_")):
            payment_ids.add(lower)
    return payment_ids


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
