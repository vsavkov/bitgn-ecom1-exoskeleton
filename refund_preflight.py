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
_EXPLICIT_PAYMENT_RE = re.compile(r"\bpay_\d+\b", re.IGNORECASE)
_EXPLICIT_RETURN_RE = re.compile(r"\bret_\d+\b", re.IGNORECASE)
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


def _current_customer_identity(vm: RuntimeVM) -> str | None:
    identity = vm.exec(ExecRequest(path="/bin/id"))
    user_id, roles = parse_runtime_identity(getattr(identity, "stdout", "") or "")
    if not user_id or not user_id.startswith("cust_") or "customer" not in roles:
        return None
    return user_id


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

    user_id = _current_customer_identity(vm)
    if user_id is None:
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

    payment_ids = [row.get("payment_id") or "" for row in rows if row.get("payment_id")]
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


def rejected_return_clarification_preflight(
    vm: RuntimeVM,
    *,
    task_text: str,
) -> RefundClarification | None:
    if not _REFUND_WORD_RE.search(task_text):
        return None

    user_id = _current_customer_identity(vm)
    if user_id is None:
        return None

    rows = _target_refund_rows(vm, task_text, user_id)
    payment_ids = {row.get("payment_id") or "" for row in rows if row.get("payment_id")}
    if len(payment_ids) != 1:
        return None

    rejected_rows = [
        row
        for row in rows
        if (row.get("return_status") or "").strip().lower() == "rejected"
    ]
    if not rejected_rows:
        return None

    row = rejected_rows[0]
    amount_cents = _row_amount_cents(row)
    if amount_cents is None:
        return None

    # A rejected return is normally a policy denial. Ask for clarification only
    # when the supplied amount is also ambiguous across this customer's payments,
    # because the user may be pointing at an unrefundable record while describing
    # another same-amount purchase.
    amount_payment_refs = _payment_refs_for_amount(
        vm,
        user_id=user_id,
        amount_cents=amount_cents,
    )
    if len(amount_payment_refs) <= 1:
        return None

    payment_id = row.get("payment_id") or ""
    return_id = row.get("return_id") or ""
    refs = dedupe_refs(
        [
            *amount_payment_refs,
            row.get("return_record_path") or "",
        ]
    )
    amount = f"EUR {amount_cents // 100}.{amount_cents % 100:02d}"
    return RefundClarification(
        completed_steps_laconic=[
            "Resolved the refund request to a single customer payment.",
            (f"Found linked return {return_id} for {payment_id} with status rejected."),
            (
                f"Found multiple {amount} payments for the current customer, "
                "so the rejected target does not identify a supported refund basis."
            ),
        ],
        message=(
            f"I found payment {payment_id} and linked return {return_id}, but that "
            f"return is rejected. I also found multiple {amount} payments for your "
            "account, so please clarify which purchase or refund basis you want me "
            "to use."
        ),
        doc_refs=["/docs/security.md", "/docs/returns.md"],
        row_refs=refs,
    )


def _target_refund_rows(
    vm: RuntimeVM,
    task_text: str,
    user_id: str,
) -> list[dict[str, str]]:
    payment_match = _EXPLICIT_PAYMENT_RE.search(task_text)
    if payment_match:
        return _payment_return_rows_for_payment_id(
            vm,
            user_id=user_id,
            payment_id=payment_match.group(0).lower(),
        )

    return_match = _EXPLICIT_RETURN_RE.search(task_text)
    if return_match:
        return _payment_return_rows_for_return_id(
            vm,
            user_id=user_id,
            return_id=return_match.group(0).lower(),
        )

    amount_cents = refund_amount_cents_from_text(task_text)
    if amount_cents is None:
        return []
    return _payment_return_rows_for_amount(
        vm,
        user_id=user_id,
        amount_cents=amount_cents,
    )


def _payment_return_select_sql() -> str:
    return (
        "select "
        "p.payment_id, p.record_path as payment_record_path, "
        "p.customer_id, p.payment_amount_cents, p.payment_currency, "
        "r.return_id, r.record_path as return_record_path, r.return_status "
        "from payment_transactions p "
        "left join return_requests r on r.payment_id = p.payment_id "
    )


def _payment_return_rows_for_payment_id(
    vm: RuntimeVM,
    *,
    user_id: str,
    payment_id: str,
) -> list[dict[str, str]]:
    return _sql_rows(
        vm,
        _payment_return_select_sql()
        + f"where p.customer_id = {sql_quote(user_id)} "
        + f"and p.payment_id = {sql_quote(payment_id)} "
        + "order by r.return_created_at desc, r.return_id;",
    )


def _payment_return_rows_for_return_id(
    vm: RuntimeVM,
    *,
    user_id: str,
    return_id: str,
) -> list[dict[str, str]]:
    return _sql_rows(
        vm,
        _payment_return_select_sql()
        + f"where p.customer_id = {sql_quote(user_id)} "
        + f"and r.return_id = {sql_quote(return_id)} "
        + "order by r.return_created_at desc, r.return_id;",
    )


def _payment_return_rows_for_amount(
    vm: RuntimeVM,
    *,
    user_id: str,
    amount_cents: int,
) -> list[dict[str, str]]:
    return _sql_rows(
        vm,
        _payment_return_select_sql()
        + f"where p.customer_id = {sql_quote(user_id)} "
        + f"and p.payment_amount_cents = {amount_cents} "
        + "and p.payment_currency = 'EUR' "
        + "order by p.payment_created_at desc, p.payment_id, "
        + "r.return_created_at desc, r.return_id;",
    )


def _row_amount_cents(row: dict[str, str]) -> int | None:
    raw_amount = (row.get("payment_amount_cents") or "").strip()
    raw_currency = (row.get("payment_currency") or "").strip().upper()
    if raw_currency != "EUR":
        return None
    try:
        return int(raw_amount)
    except ValueError:
        return None


def _payment_refs_for_amount(
    vm: RuntimeVM,
    *,
    user_id: str,
    amount_cents: int,
) -> list[str]:
    rows = _sql_rows(
        vm,
        "select payment_id, record_path, payment_created_at "
        "from payment_transactions "
        f"where customer_id = {sql_quote(user_id)} "
        f"and payment_amount_cents = {amount_cents} "
        "and payment_currency = 'EUR' "
        "order by payment_created_at desc, payment_id;",
    )
    return dedupe_refs(
        [
            row.get("record_path") or ""
            for row in rows
            if (row.get("record_path") or "").startswith("/")
        ]
    )
