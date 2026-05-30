import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest, ListRequest, ReadRequest, StatRequest
from connectrpc.errors import ConnectError

from runtime_calls import runtime_exec
from runtime_state import (
    PAYMENT_ROOTS,
    RETURN_ROOTS,
    JsonRecord,
    find_record_by_id,
    ids_equal,
    record_amount_cents,
    record_created_at,
    record_currency,
    record_id,
    record_status,
    records_for_customer,
)
from submission_refs import dedupe_refs, is_customer_identity, parse_runtime_identity


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...

    def list(self, request: ListRequest) -> Any: ...

    def read(self, request: ReadRequest) -> Any: ...

    def stat(self, request: StatRequest) -> Any: ...


@dataclass(frozen=True)
class RefundClarification:
    completed_steps_laconic: list[str]
    message: str
    doc_refs: list[str]
    row_refs: list[str]


_REFUND_WORD_RE = re.compile(r"\brefund\b", re.IGNORECASE)
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


def _current_customer_identity(vm: RuntimeVM) -> str | None:
    try:
        identity = runtime_exec(vm, ExecRequest(path="/bin/id"))
    except ConnectError:
        return None
    user_id, roles = parse_runtime_identity(getattr(identity, "stdout", "") or "")
    if not is_customer_identity(user_id) or "customer" not in roles:
        return None
    return user_id


def amount_refund_clarification_preflight(
    vm: RuntimeVM,
    *,
    task_text: str,
) -> RefundClarification | None:
    if not _REFUND_WORD_RE.search(task_text):
        return None
    if _explicit_payment_id_from_text(task_text) or _explicit_return_id_from_text(task_text):
        return None

    amount_cents = refund_amount_cents_from_text(task_text)
    if amount_cents is None:
        return None

    user_id = _current_customer_identity(vm)
    if user_id is None:
        return None

    rows = _payment_rows_for_amount(vm, user_id=user_id, amount_cents=amount_cents)
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
    payment_id = _explicit_payment_id_from_text(task_text)
    if payment_id:
        return _payment_return_rows_for_payment_id(
            vm,
            user_id=user_id,
            payment_id=payment_id,
        )

    return_id = _explicit_return_id_from_text(task_text)
    if return_id:
        return _payment_return_rows_for_return_id(
            vm,
            user_id=user_id,
            return_id=return_id,
        )

    amount_cents = refund_amount_cents_from_text(task_text)
    if amount_cents is None:
        return []
    return _payment_return_rows_for_amount(
        vm,
        user_id=user_id,
        amount_cents=amount_cents,
    )


def _payment_return_rows_for_payment_id(
    vm: RuntimeVM,
    *,
    user_id: str,
    payment_id: str,
) -> list[dict[str, str]]:
    payment = find_record_by_id(vm, PAYMENT_ROOTS, payment_id, customer_id=user_id)
    if payment is None:
        return []
    return _payment_return_rows_for_payment(vm, user_id=user_id, payment=payment)


def _payment_return_rows_for_return_id(
    vm: RuntimeVM,
    *,
    user_id: str,
    return_id: str,
) -> list[dict[str, str]]:
    return_record = find_record_by_id(vm, RETURN_ROOTS, return_id, customer_id=user_id)
    if return_record is None:
        return []
    payment_id = str(return_record.data.get("payment_id") or "")
    payment = find_record_by_id(vm, PAYMENT_ROOTS, payment_id, customer_id=user_id)
    if payment is None:
        return []
    return [_payment_return_row(payment, return_record)]


def _payment_return_rows_for_amount(
    vm: RuntimeVM,
    *,
    user_id: str,
    amount_cents: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for payment in _payment_records_for_amount(
        vm,
        user_id=user_id,
        amount_cents=amount_cents,
    ):
        rows.extend(_payment_return_rows_for_payment(vm, user_id=user_id, payment=payment))
    return sorted(
        rows,
        key=lambda row: (
            row.get("payment_created_at") or "",
            row.get("payment_id") or "",
            row.get("return_created_at") or "",
            row.get("return_id") or "",
        ),
        reverse=True,
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
    rows = _payment_rows_for_amount(vm, user_id=user_id, amount_cents=amount_cents)
    return dedupe_refs(
        [
            row.get("record_path") or ""
            for row in rows
            if (row.get("record_path") or "").startswith("/")
        ]
    )


def _payment_rows_for_amount(
    vm: RuntimeVM,
    *,
    user_id: str,
    amount_cents: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in _payment_records_for_amount(
        vm,
        user_id=user_id,
        amount_cents=amount_cents,
    ):
        rows.append(
            {
                "payment_id": record_id(record),
                "record_path": record.path,
                "payment_status": record_status(record),
                "payment_created_at": record_created_at(record),
            }
        )
    return sorted(
        rows,
        key=lambda row: (row.get("payment_created_at") or "", row.get("payment_id") or ""),
        reverse=True,
    )


def _payment_records_for_amount(
    vm: RuntimeVM,
    *,
    user_id: str,
    amount_cents: int,
) -> list[JsonRecord]:
    records: list[JsonRecord] = []
    for record in records_for_customer(vm, PAYMENT_ROOTS, user_id):
        if record_amount_cents(record) != amount_cents:
            continue
        if record_currency(record).upper() != "EUR":
            continue
        records.append(record)
    return records


def _payment_return_rows_for_payment(
    vm: RuntimeVM,
    *,
    user_id: str,
    payment: JsonRecord,
) -> list[dict[str, str]]:
    payment_id = record_id(payment)
    linked_returns = [
        return_record
        for return_record in records_for_customer(vm, RETURN_ROOTS, user_id)
        if ids_equal(return_record.data.get("payment_id"), payment_id)
    ]
    if not linked_returns:
        return [_payment_return_row(payment, None)]
    return sorted(
        [_payment_return_row(payment, return_record) for return_record in linked_returns],
        key=lambda row: (row.get("return_created_at") or "", row.get("return_id") or ""),
        reverse=True,
    )


def _payment_return_row(
    payment: JsonRecord,
    return_record: JsonRecord | None,
) -> dict[str, str]:
    row = {
        "payment_id": record_id(payment),
        "payment_record_path": payment.path,
        "customer_id": str(payment.data.get("customer_id") or ""),
        "payment_amount_cents": str(record_amount_cents(payment) or ""),
        "payment_currency": record_currency(payment),
        "payment_created_at": record_created_at(payment),
        "return_id": "",
        "return_record_path": "",
        "return_status": "",
        "return_created_at": "",
    }
    if return_record is not None:
        row.update(
            {
                "return_id": record_id(return_record),
                "return_record_path": return_record.path,
                "return_status": record_status(return_record),
                "return_created_at": record_created_at(return_record),
            }
        )
    return row


def _explicit_payment_id_from_text(task_text: str) -> str:
    return _first_identifier_with_prefix(task_text, ("pay",))


def _explicit_return_id_from_text(task_text: str) -> str:
    return _first_identifier_with_prefix(task_text, ("return", "ret"))


def _first_identifier_with_prefix(task_text: str, prefixes: tuple[str, ...]) -> str:
    for token in _identifier_tokens(task_text):
        lower = token.lower()
        for prefix in prefixes:
            if lower.startswith(f"{prefix}-") or lower.startswith(f"{prefix}_"):
                return lower
    return ""


def _identifier_tokens(task_text: str) -> list[str]:
    chars = [
        char if char.isalnum() or char in {"-", "_"} else " "
        for char in task_text
    ]
    return [token for token in "".join(chars).split() if token]
