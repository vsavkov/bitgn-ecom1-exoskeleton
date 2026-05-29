import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ReadRequest
from connectrpc.errors import ConnectError
from pydantic import BaseModel, Field


class RuntimeVM(Protocol):
    def read(self, request: ReadRequest) -> Any: ...


class ReqAnalyzeArchiveFraudExport(BaseModel):
    path: str = Field(
        description=(
            "Absolute path to the archived payment TSV export, for example "
            "/archive/payment_batch_export_abc123.tsv."
        )
    )


@dataclass(frozen=True)
class ArchivePaymentRow:
    index: int
    row_id: str
    created_at: datetime
    customer_ref: str
    store_city: str
    amount_cents: int
    currency: str
    payment_method_fingerprint: str
    device_fingerprint: str
    archive_channel: str


@dataclass(frozen=True)
class FraudRule:
    name: str
    key: str
    window_minutes: int
    min_rows: int
    min_cities: int
    min_total_cents: int = 0


@dataclass(frozen=True)
class FraudIncident:
    rule: str
    key: str
    key_value: str
    rows: tuple[ArchivePaymentRow, ...]

    @property
    def row_ids(self) -> tuple[str, ...]:
        return tuple(row.row_id for row in self.rows)

    @property
    def total_cents(self) -> int:
        return sum(row.amount_cents for row in self.rows)


RULE_SCORE_WEIGHT = {
    "rapid_customer_multicity": 50,
    "rapid_device_multicity": 70,
    "rapid_payment_multicity": 70,
    "high_value_customer_multicity": 35,
    "high_value_device_multicity": 65,
    "high_value_payment_multicity": 65,
}

CUSTOMER_CONTROLLED_CHANNELS = {"mobile_app", "web"}


FRAUD_RULES: tuple[FraudRule, ...] = (
    # Archive fraud exports do not label incidents directly. The stable signal is
    # velocity: the same customer, card fingerprint, or device fingerprint
    # appears in multiple distant store cities too quickly to be normal commerce.
    # Short windows catch scripted low-value bursts; the one-hour rules require
    # high total value so ordinary repeat customers are not pulled in.
    FraudRule(
        name="rapid_customer_multicity",
        key="customer_ref",
        window_minutes=5,
        min_rows=6,
        min_cities=3,
    ),
    FraudRule(
        name="rapid_device_multicity",
        key="device_fingerprint",
        window_minutes=5,
        min_rows=5,
        min_cities=4,
    ),
    FraudRule(
        name="rapid_payment_multicity",
        key="payment_method_fingerprint",
        window_minutes=5,
        min_rows=5,
        min_cities=4,
    ),
    FraudRule(
        name="high_value_customer_multicity",
        key="customer_ref",
        window_minutes=60,
        min_rows=4,
        min_cities=3,
        min_total_cents=150_000,
    ),
    FraudRule(
        name="high_value_device_multicity",
        key="device_fingerprint",
        window_minutes=60,
        min_rows=4,
        min_cities=3,
        min_total_cents=150_000,
    ),
    FraudRule(
        name="high_value_payment_multicity",
        key="payment_method_fingerprint",
        window_minutes=60,
        min_rows=4,
        min_cities=3,
        min_total_cents=150_000,
    ),
)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    return datetime.fromisoformat(normalized)


def _parse_archive_tsv(content: str) -> list[ArchivePaymentRow]:
    rows: list[ArchivePaymentRow] = []
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    for index, raw in enumerate(reader):
        try:
            row = ArchivePaymentRow(
                index=index,
                row_id=(raw.get("row_id") or "").strip(),
                created_at=_parse_timestamp(raw.get("created_at") or ""),
                customer_ref=(raw.get("customer_ref") or "").strip(),
                store_city=(raw.get("store_city") or "").strip(),
                amount_cents=int((raw.get("amount_cents") or "0").strip()),
                currency=(raw.get("currency") or "").strip(),
                payment_method_fingerprint=(
                    raw.get("payment_method_fingerprint") or ""
                ).strip(),
                device_fingerprint=(raw.get("device_fingerprint") or "").strip(),
                archive_channel=(raw.get("archive_channel") or "").strip(),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid archive TSV row {index + 2}") from exc

        if not row.row_id:
            raise RuntimeError(f"invalid archive TSV row {index + 2}: missing row_id")
        rows.append(row)
    return rows


def _row_key(row: ArchivePaymentRow, key: str) -> str:
    value = getattr(row, key)
    if not isinstance(value, str):
        raise RuntimeError(f"unsupported fraud grouping key: {key}")
    return value


def _has_repeated_payment_fingerprint(rows: list[ArchivePaymentRow]) -> bool:
    return len({row.payment_method_fingerprint for row in rows}) < len(rows)


def _has_repeated_customer_device_fingerprint(rows: list[ArchivePaymentRow]) -> bool:
    customer_device_rows = [
        row
        for row in rows
        if row.archive_channel in CUSTOMER_CONTROLLED_CHANNELS
        and row.device_fingerprint
    ]
    return len({row.device_fingerprint for row in customer_device_rows}) < len(
        customer_device_rows
    )


def _customer_ref_has_independent_signal(rows: list[ArchivePaymentRow]) -> bool:
    if all(row.archive_channel in CUSTOMER_CONTROLLED_CHANNELS for row in rows):
        return True

    # Service-desk archives can reuse customer refs while the operator is using a
    # shared terminal. For customer grouping, require a repeated payment or a
    # repeated customer-controlled device before treating it as fraud evidence.
    return _has_repeated_payment_fingerprint(
        rows
    ) or _has_repeated_customer_device_fingerprint(rows)


def _window_matches(rule: FraudRule, rows: list[ArchivePaymentRow]) -> bool:
    if len(rows) < rule.min_rows:
        return False
    if len({row.store_city for row in rows}) < rule.min_cities:
        return False
    if rule.key == "device_fingerprint" and not all(
        row.archive_channel in CUSTOMER_CONTROLLED_CHANNELS for row in rows
    ):
        return False
    if rule.key == "customer_ref" and not _customer_ref_has_independent_signal(rows):
        return False
    return sum(row.amount_cents for row in rows) >= rule.min_total_cents


def _incident_row_span(incident: FraudIncident) -> int:
    indexes = [row.index for row in incident.rows]
    return max(indexes) - min(indexes) + 1


def _incident_time_span_seconds(incident: FraudIncident) -> int:
    timestamps = [row.created_at for row in incident.rows]
    return int((max(timestamps) - min(timestamps)).total_seconds())


def _incident_density(incident: FraudIncident) -> float:
    return len(incident.rows) / _incident_row_span(incident)


def _dominant_channel_count(incident: FraudIncident) -> int:
    counts: dict[str, int] = defaultdict(int)
    for row in incident.rows:
        counts[row.archive_channel] += 1
    return max(counts.values(), default=0)


def _incident_score(incident: FraudIncident) -> float:
    city_count = len({row.store_city for row in incident.rows})
    compactness = _incident_density(incident)
    channel_dominance = _dominant_channel_count(incident) / len(incident.rows)
    amount_score = min(incident.total_cents / 10_000, 50)
    return (
        RULE_SCORE_WEIGHT.get(incident.rule, 0)
        + len(incident.rows) * 12
        + city_count * 8
        + compactness * 40
        + channel_dominance * 10
        + amount_score
    )


def _incident_diagnostics(incident: FraudIncident) -> dict[str, Any]:
    channel_counts: dict[str, int] = defaultdict(int)
    payment_fingerprints: set[str] = set()
    device_fingerprints: set[str] = set()
    for row in incident.rows:
        channel_counts[row.archive_channel] += 1
        payment_fingerprints.add(row.payment_method_fingerprint)
        device_fingerprints.add(row.device_fingerprint)

    return {
        "row_span": _incident_row_span(incident),
        "row_density": round(_incident_density(incident), 3),
        "time_span_seconds": _incident_time_span_seconds(incident),
        "channel_counts": dict(sorted(channel_counts.items())),
        "payment_fingerprint_count": len(payment_fingerprints),
        "device_fingerprint_count": len(device_fingerprints),
        "score": round(_incident_score(incident), 3),
    }


def _candidate_incidents_for_rule(
    rows: list[ArchivePaymentRow],
    rule: FraudRule,
) -> list[FraudIncident]:
    grouped: dict[str, list[ArchivePaymentRow]] = defaultdict(list)
    for row in rows:
        key_value = _row_key(row, rule.key)
        if key_value:
            grouped[key_value].append(row)

    incidents: list[FraudIncident] = []
    window_seconds = rule.window_minutes * 60
    for key_value, group_rows in grouped.items():
        ordered = sorted(group_rows, key=lambda row: row.created_at)
        for start, first_row in enumerate(ordered):
            window_rows = [
                row
                for row in ordered[start:]
                if (row.created_at - first_row.created_at).total_seconds()
                <= window_seconds
            ]
            if _window_matches(rule, window_rows):
                incidents.append(
                    FraudIncident(
                        rule=rule.name,
                        key=rule.key,
                        key_value=key_value,
                        rows=tuple(window_rows),
                    )
                )
    return incidents


def _candidate_incidents(rows: list[ArchivePaymentRow]) -> list[FraudIncident]:
    candidates: list[FraudIncident] = []
    for rule in FRAUD_RULES:
        candidates.extend(_candidate_incidents_for_rule(rows, rule))
    return candidates


def _drop_subset_incidents(incidents: list[FraudIncident]) -> list[FraudIncident]:
    unique_by_rows: dict[frozenset[str], FraudIncident] = {}
    for incident in incidents:
        row_set = frozenset(incident.row_ids)
        existing = unique_by_rows.get(row_set)
        if existing is None or _incident_score(incident) > _incident_score(existing):
            unique_by_rows[row_set] = incident

    unique = list(unique_by_rows.values())
    keep: list[FraudIncident] = []
    for incident in unique:
        row_set = frozenset(incident.row_ids)
        if any(
            row_set < frozenset(other.row_ids)
            for other in unique
            if other is not incident
        ):
            continue
        keep.append(incident)

    return sorted(
        keep,
        key=lambda incident: (
            min(row.index for row in incident.rows),
            -len(incident.rows),
            incident.rule,
        ),
    )


def _select_non_overlapping_incidents(
    incidents: list[FraudIncident],
) -> list[FraudIncident]:
    selected: list[FraudIncident] = []
    used_row_ids: set[str] = set()

    for incident in sorted(
        incidents,
        key=lambda item: (
            -_incident_score(item),
            min(row.index for row in item.rows),
            item.rule,
        ),
    ):
        row_ids = set(incident.row_ids)
        if row_ids & used_row_ids:
            continue
        selected.append(incident)
        used_row_ids.update(row_ids)

    return sorted(
        selected,
        key=lambda incident: (
            min(row.index for row in incident.rows),
            -len(incident.rows),
            incident.rule,
        ),
    )


def _detect_incidents(
    rows: list[ArchivePaymentRow],
) -> tuple[list[FraudIncident], list[FraudIncident]]:
    candidates = _drop_subset_incidents(_candidate_incidents(rows))
    incidents = _select_non_overlapping_incidents(candidates)
    return incidents, candidates


def detect_archive_fraud(
    rows: list[ArchivePaymentRow],
) -> tuple[list[ArchivePaymentRow], list[FraudIncident]]:
    incidents, _ = _detect_incidents(rows)
    fraud_by_id: dict[str, ArchivePaymentRow] = {}
    for incident in incidents:
        for row in incident.rows:
            fraud_by_id[row.row_id] = row

    fraud_rows = sorted(fraud_by_id.values(), key=lambda row: row.index)
    return fraud_rows, incidents


def _format_eur(cents: int) -> str:
    euros, remainder = divmod(cents, 100)
    return f"EUR {euros}.{remainder:02d}"


def _row_ref(path: str, row: ArchivePaymentRow) -> str:
    return f"{path}#row={row.row_id}"


def analyze_archive_fraud_content(path: str, content: str) -> dict[str, Any]:
    rows = _parse_archive_tsv(content)
    incidents, candidates = _detect_incidents(rows)
    fraud_by_id: dict[str, ArchivePaymentRow] = {}
    for incident in incidents:
        for row in incident.rows:
            fraud_by_id[row.row_id] = row
    fraud_rows = sorted(fraud_by_id.values(), key=lambda row: row.index)
    total_cents = sum(row.amount_cents for row in fraud_rows)
    refs = [_row_ref(path, row) for row in fraud_rows]

    return {
        "total_cents": total_cents,
        "total_message": _format_eur(total_cents),
        "fraud_row_count": len(fraud_rows),
        "fraud_row_ids": [row.row_id for row in fraud_rows],
        "refs_to_submit": refs,
        "candidate_incident_count": len(candidates),
        "selected_incident_count": len(incidents),
        "suppressed_overlapping_candidate_count": len(candidates) - len(incidents),
        "incidents": [
            {
                "rule": incident.rule,
                "key": incident.key,
                "key_value": incident.key_value,
                "row_count": len(incident.rows),
                "city_count": len({row.store_city for row in incident.rows}),
                "total_cents": incident.total_cents,
                "diagnostics": _incident_diagnostics(incident),
                "row_ids": list(incident.row_ids),
            }
            for incident in incidents
        ],
    }


def analyze_archive_fraud_export(
    vm: RuntimeVM,
    cmd: ReqAnalyzeArchiveFraudExport,
) -> dict[str, Any]:
    try:
        result = vm.read(
            ReadRequest(path=cmd.path, number=False, start_line=0, end_line=0)
        )
    except ConnectError as exc:
        raise RuntimeError(f"archive fraud read failed: {exc.message}") from exc

    if getattr(result, "truncated", False):
        raise RuntimeError(f"archive fraud export is too large to read fully: {cmd.path}")

    return analyze_archive_fraud_content(cmd.path, result.content or "")
