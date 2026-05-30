import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest

from submission_refs import dedupe_refs, parse_runtime_identity, sql_quote


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...


@dataclass(frozen=True)
class RefundClarification:
    completed_steps_laconic: list[str]
    message: str
    doc_refs: list[str]
    row_refs: list[str]


_REFUND_WORD_RE = re.compile(r"\brefund\b", re.IGNORECASE)
_EXPLICIT_REFUND_TARGET_RE = re.compile(r"\b(?:pay|ret)_\d+\b", re.IGNORECASE)
_MONEY_RE = re.compile(
    r"(?:€|eur)\s*([0-9]+(?:[.,][0-9]{1,2})?)\b|"
    r"\b([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:eur|euros?)\b",
    re.IGNORECASE,
)


def _money_to_cents(value: str) -> int:
    try:
        amount = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"invalid money amount: {value}") from exc
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def refund_amount_cents_from_text(task_text: str) -> int | None:
    match = _MONEY_RE.search(task_text)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    if not value:
        return None
    try:
        return _money_to_cents(value)
    except ValueError:
        return None


def _sql_rows(vm: RuntimeVM, query: str) -> list[dict[str, str]]:
    result = vm.exec(ExecRequest(path="/bin/sql", stdin=query))
    if getattr(result, "exit_code", 0):
        return []
    stdout = (getattr(result, "stdout", "") or "").strip()
    if not stdout:
        return []
    return [dict(row) for row in csv.DictReader(io.StringIO(stdout))]


def amount_refund_clarification_preflight(
    vm: RuntimeVM,
    *,
    task_text: str,
) -> RefundClarification | None:
    if not _REFUND_WORD_RE.search(task_text):
        return None
    if _EXPLICIT_REFUND_TARGET_RE.search(task_text):
        return None

    amount_cents = refund_amount_cents_from_text(task_text)
    if amount_cents is None:
        return None

    identity = vm.exec(ExecRequest(path="/bin/id"))
    user_id, roles = parse_runtime_identity(getattr(identity, "stdout", "") or "")
    if not user_id or not user_id.startswith("cust_") or "customer" not in roles:
        return None

    rows = _sql_rows(
        vm,
        "select payment_id, record_path, payment_status, payment_created_at "
        "from payment_transactions "
        f"where customer_id = {sql_quote(user_id)} "
        f"and payment_amount_cents = {amount_cents} "
        "and payment_currency = 'EUR' "
        "order by payment_created_at desc, payment_id;",
    )
    refs = dedupe_refs(
        [
            row.get("record_path") or ""
            for row in rows
            if (row.get("record_path") or "").startswith("/")
        ]
    )
    if len(refs) <= 1:
        return None

    payment_ids = [
        row.get("payment_id") or "" for row in rows if row.get("payment_id")
    ]
    amount = f"EUR {amount_cents // 100}.{amount_cents % 100:02d}"
    return RefundClarification(
        completed_steps_laconic=[
            f"Detected an amount-only refund request for {amount}.",
            (
                f"Found {len(refs)} matching payments for the current customer, "
                "so a unique purchase was not identified."
            ),
            "Asked for clarification instead of selecting a payment by amount alone.",
        ],
        message=(
            f"Which {amount} purchase should I refund? I found multiple matching "
            f"payments: {', '.join(payment_ids)}."
        ),
        doc_refs=["/docs/security.md", "/docs/returns.md"],
        row_refs=refs,
    )
