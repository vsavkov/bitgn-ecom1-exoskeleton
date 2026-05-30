import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest
from connectrpc.errors import ConnectError
from pydantic import BaseModel

from fraud_rules import (
    CUSTOMER_CONTROLLED_CHANNELS,
    FraudIncident,
    candidate_incidents,
    drop_subset_incidents,
    format_eur,
    incidents_summary,
    select_non_overlapping_incidents,
)
from runtime_calls import runtime_exec


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...


class ReqAnalyzePaymentFraudHistory(BaseModel):
    """No arguments needed: the helper always scans the full payment history.

    A fraud-review task either names an explicit /archive/*.tsv export or asks
    about current/archived payment history in the runtime. This helper covers
    the runtime payment-history path, including archived basket references
    stored in /proc/payments; pass explicit archive TSV paths to
    analyze_archive_fraud_export instead.
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


PAYMENT_FRAUD_PAGE_SIZE = 90
LIVE_CITY_HOP_WINDOW_SECONDS = 10 * 60
LIVE_CITY_HOP_BATCH_WINDOW_SECONDS = 3 * 60 * 60
LIVE_CITY_HOP_BATCH_MIN_INCIDENTS = 3
LIVE_CITY_HOP_STANDALONE_MIN_ROWS = 4
PAYMENT_FRAUD_SELECT = (
    "select "
    "p.payment_id, p.record_path, p.customer_id, "
    "p.payment_amount_cents, p.payment_currency, p.payment_created_at, "
    "p.payment_method_fingerprint, p.device_fingerprint, s.city "
    "from payment_transactions p "
    "join stores s on s.store_id = p.store_id "
    "order by p.payment_created_at, p.payment_id"
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
        result = runtime_exec(vm, ExecRequest(path="/bin/sql", stdin=SCHEMA_PROBE_SQL))
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


def _payment_fraud_sql_page(offset: int) -> str:
    return (
        f"{PAYMENT_FRAUD_SELECT} "
        f"limit {PAYMENT_FRAUD_PAGE_SIZE} offset {offset};"
    )


def _parse_payment_rows_page(
    stdout: str,
    *,
    start_index: int,
) -> tuple[list[PaymentTransactionRow], int, list[str]]:
    parsed: list[PaymentTransactionRow] = []
    skipped_errors: list[str] = []
    reader = csv.DictReader(io.StringIO(stdout))
    physical_count = 0

    for page_index, raw in enumerate(reader):
        physical_count += 1
        try:
            payment_id = (raw.get("payment_id") or "").strip()
            record_path = (raw.get("record_path") or "").strip()
            row = PaymentTransactionRow(
                index=start_index + page_index,
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
        except (TypeError, ValueError) as exc:
            skipped_errors.append(f"row {start_index + page_index + 2}: {exc}")
            continue
        if not row.row_id:
            continue
        parsed.append(row)

    return parsed, physical_count, skipped_errors


def _fetch_payment_rows(vm: RuntimeVM) -> PaymentFraudFetchResult:
    schema_warning = _probe_schema(vm)
    if schema_warning:
        return PaymentFraudFetchResult(rows=[], warning=schema_warning)

    parsed: list[PaymentTransactionRow] = []
    consumed_count = 0
    skipped_count = 0
    skipped_errors: list[str] = []

    # /bin/sql can truncate large CSV outputs and append a warning row. Page the
    # history explicitly so fraud detection sees the whole timeline.
    for offset in range(0, 100_000, PAYMENT_FRAUD_PAGE_SIZE):
        try:
            result = runtime_exec(
                vm,
                ExecRequest(path="/bin/sql", stdin=_payment_fraud_sql_page(offset))
            )
        except ConnectError as exc:
            return PaymentFraudFetchResult(
                rows=parsed,
                warning=f"SQL failed: {exc.message}",
            )

        if getattr(result, "exit_code", 0):
            return PaymentFraudFetchResult(
                rows=parsed,
                warning=(
                    "SQL exited with "
                    f"{result.exit_code}: {(result.stderr or '').strip()}"
                ),
            )

        stdout = (result.stdout or "").strip()
        if not stdout:
            break

        try:
            page_rows, physical_count, page_errors = _parse_payment_rows_page(
                stdout,
                start_index=consumed_count,
            )
        except csv.Error as exc:
            return PaymentFraudFetchResult(
                rows=parsed,
                warning=f"payment_transactions parse error: {exc}",
            )

        parsed.extend(page_rows)
        consumed_count += physical_count
        skipped_count += len(page_errors)
        skipped_errors.extend(page_errors[: max(0, 3 - len(skipped_errors))])

        if physical_count < PAYMENT_FRAUD_PAGE_SIZE:
            break

    warning = ""
    if skipped_count:
        warning = (
            f"skipped {skipped_count} malformed payment row(s): "
            + "; ".join(skipped_errors)
        )
    return PaymentFraudFetchResult(rows=parsed, warning=warning)


def _payment_record_ref(row: PaymentTransactionRow) -> str:
    if row.record_path.startswith("/"):
        return row.record_path
    return f"/proc/payments/{row.row_id}.json"


def _live_city_hop_incidents(
    rows: list[PaymentTransactionRow],
) -> list[FraudIncident]:
    grouped: dict[tuple[str, str], list[PaymentTransactionRow]] = defaultdict(list)
    for row in rows:
        if not row.customer_ref:
            continue
        for signal in (row.payment_method_fingerprint, row.device_fingerprint):
            if signal:
                grouped[(row.customer_ref, signal)].append(row)

    incidents: list[FraudIncident] = []
    for (customer_ref, _payment_method), group_rows in grouped.items():
        ordered = sorted(group_rows, key=lambda row: (row.created_at, row.row_id))
        chain: list[PaymentTransactionRow] = []

        def flush_chain() -> None:
            if len(chain) < 2:
                return
            if len({row.store_city for row in chain}) < 2:
                return
            incidents.append(
                FraudIncident(
                    rule="live_customer_city_hop",
                    key="customer_ref",
                    key_value=customer_ref,
                    rows=tuple(chain),
                )
            )

        for row in ordered:
            if not chain:
                chain = [row]
                continue

            previous = chain[-1]
            delta_seconds = (row.created_at - previous.created_at).total_seconds()
            if 0 <= delta_seconds <= LIVE_CITY_HOP_WINDOW_SECONDS:
                chain.append(row)
                continue

            # Live archived-history fraud can appear as a chain of two or more
            # same-card payments hopping between cities too quickly for a
            # person to travel. Finalize the chain when the timing gap breaks.
            flush_chain()
            chain = [row]

        flush_chain()

    return incidents


def _dedupe_incidents_by_rows(incidents: list[FraudIncident]) -> list[FraudIncident]:
    by_rows: dict[tuple[str, ...], FraudIncident] = {}
    for incident in incidents:
        by_rows.setdefault(tuple(sorted(incident.row_ids)), incident)
    return list(by_rows.values())


def _incident_start(incident: FraudIncident) -> datetime:
    return min(row.created_at for row in incident.rows)


def _batched_short_city_hop_incidents(
    incidents: list[FraudIncident],
) -> list[FraudIncident]:
    batched_by_rows: dict[tuple[str, ...], FraudIncident] = {}
    ordered = sorted(incidents, key=_incident_start)

    for index, incident in enumerate(ordered):
        window_start = _incident_start(incident)
        window_incidents = [
            other
            for other in ordered[index:]
            if (
                _incident_start(other) - window_start
            ).total_seconds()
            <= LIVE_CITY_HOP_BATCH_WINDOW_SECONDS
        ]
        if len(window_incidents) < LIVE_CITY_HOP_BATCH_MIN_INCIDENTS:
            continue

        for batched in window_incidents:
            batched_by_rows[tuple(sorted(batched.row_ids))] = batched

    return list(batched_by_rows.values())


def _filter_live_city_hop_incidents(
    city_hop_incidents: list[FraudIncident],
    strong_incidents: list[FraudIncident],
) -> list[FraudIncident]:
    unique_incidents = _dedupe_incidents_by_rows(city_hop_incidents)
    strong_row_sets = [set(incident.row_ids) for incident in strong_incidents]
    large_chains: list[FraudIncident] = []
    short_chains: list[FraudIncident] = []

    for incident in unique_incidents:
        if any(set(incident.row_ids) <= row_set for row_set in strong_row_sets):
            continue
        if len(incident.rows) >= LIVE_CITY_HOP_STANDALONE_MIN_ROWS:
            large_chains.append(incident)
            continue
        short_chains.append(incident)

    # One short city-hop chain can be noise. A dense batch of the same pattern
    # across several customers is a campaign-level signal in the archived
    # payment history, so keep those chains as one fraud hit.
    return [*large_chains, *_batched_short_city_hop_incidents(short_chains)]


def _detect_live_payment_fraud(
    rows: list[PaymentTransactionRow],
) -> tuple[list[PaymentTransactionRow], list[FraudIncident], list[FraudIncident]]:
    strong_incidents = candidate_incidents(list(rows))  # type: ignore[arg-type]
    city_hop_incidents = _filter_live_city_hop_incidents(
        _live_city_hop_incidents(rows),
        strong_incidents,
    )
    candidates = drop_subset_incidents(
        [
            *strong_incidents,
            *city_hop_incidents,
        ]
    )
    incidents = select_non_overlapping_incidents(candidates)
    fraud_by_id: dict[str, PaymentTransactionRow] = {}
    for incident in incidents:
        for row in incident.rows:
            if isinstance(row, PaymentTransactionRow):
                fraud_by_id[row.row_id] = row
    fraud_rows = sorted(fraud_by_id.values(), key=lambda row: row.index)
    return fraud_rows, incidents, candidates


def analyze_payment_fraud_history(
    vm: RuntimeVM,
    cmd: ReqAnalyzePaymentFraudHistory,  # noqa: ARG001  # documented intentionally empty
) -> dict[str, Any]:
    fetch = _fetch_payment_rows(vm)
    rows = fetch.rows
    fraud_rows, incidents, candidates = _detect_live_payment_fraud(rows)
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
        if rows:
            payload["warning"] = fetch.warning
        else:
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
