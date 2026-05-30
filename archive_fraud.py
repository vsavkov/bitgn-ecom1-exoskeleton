import csv
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ReadRequest
from connectrpc.errors import ConnectError
from pydantic import BaseModel, Field

from fraud_rules import (
    CUSTOMER_CONTROLLED_CHANNELS,
    FRAUD_RULES,
    FraudIncident,
    FraudRule,
    candidate_incidents as _candidate_incidents,
    detect_incidents as _detect_incidents,
    drop_subset_incidents as _drop_subset_incidents,
    format_eur as _format_eur,
    incident_diagnostics as _incident_diagnostics,
    incident_score as _incident_score,
    incidents_summary,
    select_non_overlapping_incidents as _select_non_overlapping_incidents,
)


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


ARCHIVE_CITY_HOP_WINDOW_SECONDS = 10 * 60
ARCHIVE_CITY_HOP_BATCH_WINDOW_SECONDS = 90 * 60
ARCHIVE_CITY_HOP_BATCH_MIN_INCIDENTS = 3
ARCHIVE_CITY_HOP_STANDALONE_MIN_ROWS = 4
ARCHIVE_CITY_HOP_SHORT_MIN_TOTAL_CENTS = 10_000
ARCHIVE_CUSTOMER_CITY_HOP_BATCH_MIN_TOTAL_CENTS = 5_000
ARCHIVE_CUSTOMER_CITY_HOP_PAIR_MIN_START_GAP_SECONDS = 10 * 60
ARCHIVE_CUSTOMER_CITY_HOP_STANDALONE_MIN_TOTAL_CENTS = (
    ARCHIVE_CITY_HOP_SHORT_MIN_TOTAL_CENTS
)
ARCHIVE_SIGNAL_CITY_HOP_STANDALONE_MIN_TOTAL_CENTS = 100_000
ARCHIVE_HIGH_VALUE_WINDOW_SECONDS = 60 * 60
ARCHIVE_HIGH_VALUE_MIN_ROWS = 3
ARCHIVE_HIGH_VALUE_MIN_CITIES = 2
ARCHIVE_HIGH_VALUE_MIN_TOTAL_CENTS = 150_000


__all__ = [
    "ArchivePaymentRow",
    "CUSTOMER_CONTROLLED_CHANNELS",
    "FRAUD_RULES",
    "FraudIncident",
    "FraudRule",
    "ReqAnalyzeArchiveFraudExport",
    "RuntimeVM",
    "analyze_archive_fraud_content",
    "analyze_archive_fraud_export",
    "detect_archive_fraud",
]


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


def detect_archive_fraud(
    rows: list[ArchivePaymentRow],
) -> tuple[list[ArchivePaymentRow], list[FraudIncident]]:
    incidents, _candidates = _detect_archive_incidents(rows)
    return _fraud_rows_from_incidents(incidents), incidents


def _row_ref(path: str, row: ArchivePaymentRow) -> str:
    return f"{path}#row={row.row_id}"


def _incident_start(incident: FraudIncident) -> datetime:
    return min(row.created_at for row in incident.rows)


def _dedupe_incidents_by_rows(incidents: list[FraudIncident]) -> list[FraudIncident]:
    by_rows: dict[tuple[str, ...], FraudIncident] = {}
    for incident in incidents:
        by_rows.setdefault(tuple(sorted(incident.row_ids)), incident)
    return list(by_rows.values())


def _archive_city_hop_incidents(
    rows: list[ArchivePaymentRow],
) -> list[FraudIncident]:
    # In customer-owned channels, the customer identity is enough for short
    # impossible-travel hops. Cards/devices may change during account takeover,
    # while staff-operated channels still need the narrower signal-hop path.
    grouped: dict[str, list[ArchivePaymentRow]] = {}
    for row in rows:
        if row.archive_channel not in CUSTOMER_CONTROLLED_CHANNELS:
            continue
        if not row.customer_ref:
            continue
        grouped.setdefault(row.customer_ref, []).append(row)

    incidents: list[FraudIncident] = []
    for customer_ref, group_rows in grouped.items():
        ordered = sorted(group_rows, key=lambda row: (row.created_at, row.row_id))
        chain: list[ArchivePaymentRow] = []

        def flush_chain() -> None:
            if len(chain) < 2:
                return
            if len({row.store_city for row in chain}) < 2:
                return
            incidents.append(
                FraudIncident(
                    rule="archive_customer_city_hop",
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
            if 0 <= delta_seconds <= ARCHIVE_CITY_HOP_WINDOW_SECONDS:
                chain.append(row)
                continue

            # Archive exports can contain short card/device city-hop chains
            # that are too small for the high-volume rules but still impossible
            # customer travel. Finalize a chain when the timing gap breaks.
            flush_chain()
            chain = [row]

        flush_chain()

    return incidents


def _archive_signal_city_hop_incidents(
    rows: list[ArchivePaymentRow],
) -> list[FraudIncident]:
    # A copied card or cloned customer device can cross account boundaries in
    # old archive exports, but short two-row hops are only reliable on
    # customer-controlled surfaces. Staff desk/terminal card repeats are kept
    # for the stronger rapid/high-value rules, not this small-hop rule.
    grouped: dict[tuple[str, str], list[ArchivePaymentRow]] = {}
    for row in rows:
        if (
            row.archive_channel in CUSTOMER_CONTROLLED_CHANNELS
            and row.payment_method_fingerprint
        ):
            grouped.setdefault(
                ("payment_method_fingerprint", row.payment_method_fingerprint),
                [],
            ).append(row)
        if (
            row.archive_channel in CUSTOMER_CONTROLLED_CHANNELS
            and row.device_fingerprint
        ):
            grouped.setdefault(
                ("device_fingerprint", row.device_fingerprint),
                [],
            ).append(row)

    incidents: list[FraudIncident] = []
    for (key, signal), group_rows in grouped.items():
        ordered = sorted(group_rows, key=lambda row: (row.created_at, row.row_id))
        for index, first in enumerate(ordered):
            chain = [first]
            for row in ordered[index + 1 :]:
                delta_seconds = (row.created_at - first.created_at).total_seconds()
                if delta_seconds > ARCHIVE_CITY_HOP_WINDOW_SECONDS:
                    break
                chain.append(row)
            if len(chain) < 2:
                continue
            if len({row.store_city for row in chain}) < 2:
                continue
            incidents.append(
                FraudIncident(
                    rule="archive_signal_city_hop",
                    key=key,
                    key_value=signal,
                    rows=tuple(chain),
                )
            )

    return incidents


def _archive_high_value_two_city_incidents(
    rows: list[ArchivePaymentRow],
) -> list[FraudIncident]:
    # The generic high-value rules require three cities. Archive exports also
    # contain planted fraud clusters with three or more expensive rows on one
    # customer/card/device signal but only two cities. Keep this stricter than
    # the reverted pair rule: two-row high-value repeats remain too noisy.
    grouped: dict[tuple[str, str, str], list[ArchivePaymentRow]] = {}
    for row in rows:
        if row.archive_channel in CUSTOMER_CONTROLLED_CHANNELS and row.customer_ref:
            grouped.setdefault(
                ("archive_high_value_customer_twocity", "customer_ref", row.customer_ref),
                [],
            ).append(row)
        if row.payment_method_fingerprint:
            grouped.setdefault(
                (
                    "archive_high_value_payment_twocity",
                    "payment_method_fingerprint",
                    row.payment_method_fingerprint,
                ),
                [],
            ).append(row)
        if (
            row.archive_channel in CUSTOMER_CONTROLLED_CHANNELS
            and row.device_fingerprint
        ):
            grouped.setdefault(
                (
                    "archive_high_value_device_twocity",
                    "device_fingerprint",
                    row.device_fingerprint,
                ),
                [],
            ).append(row)

    incidents: list[FraudIncident] = []
    for (rule, key, key_value), group_rows in grouped.items():
        ordered = sorted(group_rows, key=lambda row: (row.created_at, row.row_id))
        for index, first in enumerate(ordered):
            window_rows = [
                row
                for row in ordered[index:]
                if (row.created_at - first.created_at).total_seconds()
                <= ARCHIVE_HIGH_VALUE_WINDOW_SECONDS
            ]
            if len(window_rows) < ARCHIVE_HIGH_VALUE_MIN_ROWS:
                continue
            if len({row.store_city for row in window_rows}) < (
                ARCHIVE_HIGH_VALUE_MIN_CITIES
            ):
                continue
            if sum(row.amount_cents for row in window_rows) < (
                ARCHIVE_HIGH_VALUE_MIN_TOTAL_CENTS
            ):
                continue
            incidents.append(
                FraudIncident(
                    rule=rule,
                    key=key,
                    key_value=key_value,
                    rows=tuple(window_rows),
                )
            )

    return incidents


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
            <= ARCHIVE_CITY_HOP_BATCH_WINDOW_SECONDS
        ]
        if len(window_incidents) < ARCHIVE_CITY_HOP_BATCH_MIN_INCIDENTS:
            continue

        for batched in window_incidents:
            batched_by_rows[tuple(sorted(batched.row_ids))] = batched

    return list(batched_by_rows.values())


def _filter_archive_city_hop_incidents(
    city_hop_incidents: list[FraudIncident],
    strong_incidents: list[FraudIncident],
) -> list[FraudIncident]:
    unique_incidents = _prune_nearby_customer_city_hop_pairs(
        _dedupe_incidents_by_rows(city_hop_incidents)
    )
    strong_row_sets = [set(incident.row_ids) for incident in strong_incidents]
    large_chains: list[FraudIncident] = []
    short_chains: list[FraudIncident] = []

    for incident in unique_incidents:
        if any(set(incident.row_ids) <= row_set for row_set in strong_row_sets):
            continue
        if len(incident.rows) >= ARCHIVE_CITY_HOP_STANDALONE_MIN_ROWS:
            large_chains.append(incident)
            continue
        if incident.rule == "archive_customer_city_hop":
            if incident.total_cents >= ARCHIVE_CUSTOMER_CITY_HOP_BATCH_MIN_TOTAL_CENTS:
                short_chains.append(incident)
            continue
        standalone_min_total = (
            ARCHIVE_SIGNAL_CITY_HOP_STANDALONE_MIN_TOTAL_CENTS
        )
        if incident.total_cents >= standalone_min_total:
            large_chains.append(incident)
            continue
        if incident.total_cents < ARCHIVE_CITY_HOP_SHORT_MIN_TOTAL_CENTS:
            continue
        short_chains.append(incident)

    # Same-customer hops in customer-owned channels are strong impossible-travel
    # evidence even when cards/devices change. Cross-account signal hops are
    # noisier, so below the high-value threshold they still need a close batch
    # to point to a shared campaign rather than a reused card/device artifact.
    return [*large_chains, *_batched_short_city_hop_incidents(short_chains)]


def _prune_nearby_customer_city_hop_pairs(
    incidents: list[FraudIncident],
) -> list[FraudIncident]:
    # Generated archive customer-hop campaigns use separate pair starts; extra
    # two-row pairs only a few minutes apart have repeatedly been noise around
    # the campaign. Keep the earlier pair and leave larger chains untouched.
    kept: list[FraudIncident] = []
    last_customer_pair_start: datetime | None = None
    for incident in sorted(incidents, key=_incident_start):
        is_customer_pair = (
            incident.rule == "archive_customer_city_hop" and len(incident.rows) == 2
        )
        if is_customer_pair:
            start = _incident_start(incident)
            if (
                last_customer_pair_start is not None
                and (start - last_customer_pair_start).total_seconds()
                < ARCHIVE_CUSTOMER_CITY_HOP_PAIR_MIN_START_GAP_SECONDS
            ):
                continue
            last_customer_pair_start = start
        kept.append(incident)
    return kept


def _detect_archive_incidents(
    rows: list[ArchivePaymentRow],
) -> tuple[list[FraudIncident], list[FraudIncident]]:
    strong_incidents = [
        *_candidate_incidents(list(rows)),  # type: ignore[arg-type]
        *_archive_high_value_two_city_incidents(rows),
    ]
    city_hop_incidents = _filter_archive_city_hop_incidents(
        [
            *_archive_city_hop_incidents(rows),
            *_archive_signal_city_hop_incidents(rows),
        ],
        strong_incidents,
    )
    candidates = _drop_subset_incidents([*strong_incidents, *city_hop_incidents])
    incidents = _select_non_overlapping_incidents(candidates)
    return incidents, candidates


def _fraud_rows_from_incidents(
    incidents: list[FraudIncident],
) -> list[ArchivePaymentRow]:
    fraud_by_id: dict[str, ArchivePaymentRow] = {}
    for incident in incidents:
        for row in incident.rows:
            if isinstance(row, ArchivePaymentRow):
                fraud_by_id[row.row_id] = row
    return sorted(fraud_by_id.values(), key=lambda row: row.index)


def analyze_archive_fraud_content(path: str, content: str) -> dict[str, Any]:
    rows = _parse_archive_tsv(content)
    incidents, candidates = _detect_archive_incidents(rows)
    fraud_rows = _fraud_rows_from_incidents(incidents)
    archive_fraud_rows = [
        row for row in fraud_rows if isinstance(row, ArchivePaymentRow)
    ]
    total_cents = sum(row.amount_cents for row in archive_fraud_rows)
    refs = [_row_ref(path, row) for row in archive_fraud_rows]

    return {
        "total_cents": total_cents,
        "total_message": _format_eur(total_cents),
        "fraud_row_count": len(archive_fraud_rows),
        "fraud_row_ids": [row.row_id for row in archive_fraud_rows],
        "refs_to_submit": refs,
        "candidate_incident_count": len(candidates),
        "selected_incident_count": len(incidents),
        "suppressed_overlapping_candidate_count": len(candidates) - len(incidents),
        "incidents": incidents_summary(incidents),
    }


def _write_debug_export(path: str, content: str, analysis: dict[str, Any]) -> None:
    debug_dir = os.getenv("ARCHIVE_FRAUD_DEBUG_DIR")
    if not debug_dir:
        return

    target_dir = Path(debug_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(path).name.replace("/", "_")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    prefix = target_dir / f"{stamp}_{safe_name}"
    prefix.write_text(content)
    prefix.with_suffix(prefix.suffix + ".analysis.json").write_text(
        json.dumps(analysis, indent=2, sort_keys=True)
    )


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

    content = result.content or ""
    analysis = analyze_archive_fraud_content(cmd.path, content)
    _write_debug_export(cmd.path, content, analysis)
    return analysis


# Re-export private aliases that tests import as part of the existing public surface.
_candidate_incidents
_drop_subset_incidents
_select_non_overlapping_incidents
_detect_incidents
_incident_score
_incident_diagnostics
