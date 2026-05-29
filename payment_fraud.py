import csv
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest
from connectrpc.errors import ConnectError
from pydantic import BaseModel

from fraud_rules import (
    CUSTOMER_CONTROLLED_CHANNELS,
    FraudIncident,
    detect_fraud_rows,
    format_eur,
    incidents_summary,
)


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...


class ReqAnalyzePaymentFraudHistory(BaseModel):
    """No arguments needed: the helper always scans the full payment history.

    A fraud-review task either asks about the live /proc/payments transactions
    or about an archived /archive/*.tsv export. This helper covers the live
    path; pass the archive path to analyze_archive_fraud_export instead.
    """


@dataclass(frozen=True)
class PaymentTransactionRow:
    index: int
    row_id: str  # payment_id, used both as identifier and ref leaf
    created_at: datetime
    customer_ref: str
    store_city: str
    amount_cents: int
    currency: str
    payment_method_fingerprint: str
    device_fingerprint: str
    archive_channel: str  # always a customer-controlled hint for live history
    record_path: str


# Live payments come from the customer-controlled checkout flow, so we mark
# the rows with a synthetic channel that passes the device/customer signal
# gates inside fraud_rules.
LIVE_PAYMENT_CHANNEL = "customer_terminal"
assert LIVE_PAYMENT_CHANNEL in CUSTOMER_CONTROLLED_CHANNELS


PAYMENT_FRAUD_SQL = (
    "select "
    "p.payment_id, p.record_path, p.customer_id, "
    "p.payment_amount_cents, p.payment_currency, p.payment_created_at, "
    "p.payment_method_fingerprint, p.device_fingerprint, s.city "
    "from payment_transactions p "
    "join stores s on s.store_id = p.store_id "
    "order by p.payment_created_at, p.payment_id;"
)

REQUIRED_FRAUD_TABLES = ("payment_transactions", "stores")
SCHEMA_PROBE_SQL = (
    "select name from sqlite_schema where type = 'table' "
    "and name in ('payment_transactions','stores') order by name;"
)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    return datetime.fromisoformat(normalized)


@dataclass(frozen=True)
class PaymentFraudFetchResult:
    rows: list[PaymentTransactionRow]
    warning: str = ""


def _probe_schema(vm: RuntimeVM) -> str:
    # A trial-specific snapshot can rename payment_transactions or drop the
    # stores join. Probe sqlite_schema first so the heavy fraud query never
    # runs in a snapshot where it cannot succeed (and risks long-running
    # full-table scans before the runtime kills the trial).
    try:
        result = vm.exec(ExecRequest(path="/bin/sql", stdin=SCHEMA_PROBE_SQL))
    except ConnectError as exc:
        return f"sqlite_schema probe failed: {exc.message}"
    if getattr(result, "exit_code", 0):
        return (
            "sqlite_schema probe exit "
            f"{result.exit_code}: {(result.stderr or '').strip()}"
        )

    stdout = (result.stdout or "").strip()
    found: set[str] = set()
    try:
        reader = csv.DictReader(io.StringIO(stdout))
        for row in reader:
            name = (row.get("name") or "").strip()
            if name:
                found.add(name)
    except csv.Error as exc:
        return f"sqlite_schema parse error: {exc}"

    missing = [name for name in REQUIRED_FRAUD_TABLES if name not in found]
    if missing:
        return f"sqlite_schema is missing required tables: {', '.join(missing)}"
    return ""


def _fetch_payment_rows(vm: RuntimeVM) -> PaymentFraudFetchResult:
    schema_warning = _probe_schema(vm)
    if schema_warning:
        return PaymentFraudFetchResult(rows=[], warning=schema_warning)

    try:
        result = vm.exec(ExecRequest(path="/bin/sql", stdin=PAYMENT_FRAUD_SQL))
    except ConnectError as exc:
        return PaymentFraudFetchResult(rows=[], warning=f"SQL failed: {exc.message}")

    if getattr(result, "exit_code", 0):
        return PaymentFraudFetchResult(
            rows=[],
            warning=(
                "SQL exited with "
                f"{result.exit_code}: {(result.stderr or '').strip()}"
            ),
        )

    stdout = (result.stdout or "").strip()
    if not stdout:
        return PaymentFraudFetchResult(rows=[])

    parsed: list[PaymentTransactionRow] = []
    try:
        reader = csv.DictReader(io.StringIO(stdout))
        for index, raw in enumerate(reader):
            payment_id = (raw.get("payment_id") or "").strip()
            record_path = (raw.get("record_path") or "").strip()
            row = PaymentTransactionRow(
                index=index,
                row_id=payment_id,
                created_at=_parse_timestamp(raw.get("payment_created_at") or ""),
                customer_ref=(raw.get("customer_id") or "").strip(),
                store_city=(raw.get("city") or "").strip(),
                amount_cents=int((raw.get("payment_amount_cents") or "0").strip()),
                currency=(raw.get("payment_currency") or "").strip(),
                payment_method_fingerprint=(
                    raw.get("payment_method_fingerprint") or ""
                ).strip(),
                device_fingerprint=(raw.get("device_fingerprint") or "").strip(),
                archive_channel=LIVE_PAYMENT_CHANNEL,
                record_path=record_path,
            )
            if not row.row_id:
                continue
            parsed.append(row)
    except (TypeError, ValueError, csv.Error) as exc:
        return PaymentFraudFetchResult(
            rows=[],
            warning=f"payment_transactions parse error: {exc}",
        )

    return PaymentFraudFetchResult(rows=parsed)


def _payment_record_ref(row: PaymentTransactionRow) -> str:
    if row.record_path.startswith("/"):
        return row.record_path
    return f"/proc/payments/{row.row_id}.json"


def analyze_payment_fraud_history(
    vm: RuntimeVM,
    cmd: ReqAnalyzePaymentFraudHistory,  # noqa: ARG001  # documented intentionally empty
) -> dict[str, Any]:
    fetch = _fetch_payment_rows(vm)
    rows = fetch.rows
    fraud_rows, incidents, candidates = detect_fraud_rows(
        list(rows)  # type: ignore[arg-type]
    )
    payment_fraud_rows = [
        row for row in fraud_rows if isinstance(row, PaymentTransactionRow)
    ]
    total_cents = sum(row.amount_cents for row in payment_fraud_rows)
    refs = [_payment_record_ref(row) for row in payment_fraud_rows]

    payload: dict[str, Any] = {
        "total_cents": total_cents,
        # Only claim a total_message when we actually have rows. Otherwise the
        # shared EvidenceLedger.fraud_total_message bucket would overwrite a
        # neighbouring archive-fraud total with a spurious "EUR 0.00".
        "total_message": format_eur(total_cents) if payment_fraud_rows else "",
        "fraud_payment_count": len(payment_fraud_rows),
        "fraud_payment_ids": [row.row_id for row in payment_fraud_rows],
        "refs_to_submit": refs,
        "candidate_incident_count": len(candidates),
        "selected_incident_count": len(incidents),
        "suppressed_overlapping_candidate_count": len(candidates) - len(incidents),
        "incidents": incidents_summary(incidents),
    }
    if fetch.warning:
        payload["warning"] = (
            f"{fetch.warning}; the live payment history could not be loaded, "
            "fall back to manual SQL on the current schema if needed."
        )
    return payload


__all__ = [
    "FraudIncident",
    "LIVE_PAYMENT_CHANNEL",
    "PaymentTransactionRow",
    "ReqAnalyzePaymentFraudHistory",
    "RuntimeVM",
    "analyze_payment_fraud_history",
]
