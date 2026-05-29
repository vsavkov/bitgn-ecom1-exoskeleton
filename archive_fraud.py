import csv
import io
from dataclasses import dataclass
from datetime import datetime
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
    detect_fraud_rows,
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
    fraud_rows, incidents, _candidates = detect_fraud_rows(
        list(rows)  # type: ignore[arg-type]
    )
    return [row for row in fraud_rows if isinstance(row, ArchivePaymentRow)], incidents


def _row_ref(path: str, row: ArchivePaymentRow) -> str:
    return f"{path}#row={row.row_id}"


def analyze_archive_fraud_content(path: str, content: str) -> dict[str, Any]:
    rows = _parse_archive_tsv(content)
    fraud_rows, incidents, candidates = detect_fraud_rows(
        list(rows)  # type: ignore[arg-type]
    )
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


# Re-export private aliases that tests import as part of the existing public surface.
_candidate_incidents
_drop_subset_incidents
_select_non_overlapping_incidents
_detect_incidents
_incident_score
_incident_diagnostics
