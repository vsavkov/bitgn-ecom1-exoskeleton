from dataclasses import dataclass
from datetime import datetime, timezone

from fraud_rules import detect_fraud_rows
from payment_fraud import (
    LIVE_PAYMENT_CHANNEL,
    PaymentTransactionRow,
    ReqAnalyzePaymentFraudHistory,
    analyze_payment_fraud_history,
)


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


SCHEMA_OK_PROBE_STDOUT = "name\npayment_transactions\nstores\n"
SCHEMA_MISSING_PROBE_STDOUT = "name\n"


class FakeVM:
    def __init__(
        self,
        *,
        sql_rows: str = "",
        exit_code: int = 0,
        schema_probe_stdout: str = SCHEMA_OK_PROBE_STDOUT,
        schema_probe_exit: int = 0,
    ) -> None:
        self.sql_rows = sql_rows
        self.exit_code = exit_code
        self.schema_probe_stdout = schema_probe_stdout
        self.schema_probe_exit = schema_probe_exit
        self.queries: list[str] = []

    def exec(self, request) -> ExecResult:
        if request.path != "/bin/sql":
            raise AssertionError(f"unexpected exec path: {request.path}")
        self.queries.append(request.stdin)
        if "from sqlite_schema" in request.stdin:
            return ExecResult(
                stdout=self.schema_probe_stdout,
                exit_code=self.schema_probe_exit,
            )
        return ExecResult(stdout=self.sql_rows, exit_code=self.exit_code)


def _row(
    *,
    index: int,
    payment_id: str,
    city: str,
    customer: str = "cust_burst",
    pm: str = "pm_BURST",
    device: str = "dev_BURST",
    minute_offset: int = 0,
    amount_cents: int = 5000,
) -> PaymentTransactionRow:
    base = datetime(2024, 7, 4, 10, 0, 0, tzinfo=timezone.utc)
    return PaymentTransactionRow(
        index=index,
        row_id=payment_id,
        created_at=base.replace(minute=minute_offset),
        customer_ref=customer,
        store_city=city,
        amount_cents=amount_cents,
        currency="EUR",
        payment_method_fingerprint=pm,
        device_fingerprint=device,
        archive_channel=LIVE_PAYMENT_CHANNEL,
        record_path=f"/proc/payments/{payment_id}.json",
    )


def test_live_payments_pass_customer_device_channel_gate() -> None:
    burst_rows = [
        _row(index=0, payment_id="pay_001", city="Vienna", minute_offset=0),
        _row(index=1, payment_id="pay_002", city="Graz", minute_offset=1),
        _row(index=2, payment_id="pay_003", city="Linz", minute_offset=2),
        _row(index=3, payment_id="pay_004", city="Brno", minute_offset=3),
        _row(index=4, payment_id="pay_005", city="Bratislava", minute_offset=4),
    ]

    fraud_rows, incidents, _ = detect_fraud_rows(list(burst_rows))

    assert len(incidents) > 0
    assert {row.row_id for row in fraud_rows} == {
        "pay_001",
        "pay_002",
        "pay_003",
        "pay_004",
        "pay_005",
    }


def test_analyze_payment_fraud_history_returns_refs_and_total() -> None:
    csv_header = (
        "payment_id,record_path,customer_id,payment_amount_cents,payment_currency,"
        "payment_created_at,payment_method_fingerprint,device_fingerprint,city"
    )
    csv_rows = [
        # Fraud burst: 5 cities in 4 minutes on the same card
        "pay_010,/proc/payments/pay_010.json,cust_X,40000,EUR,2024-07-04T10:00:00Z,pm_X,dev_X,Vienna",
        "pay_011,/proc/payments/pay_011.json,cust_X,42000,EUR,2024-07-04T10:01:00Z,pm_X,dev_X,Graz",
        "pay_012,/proc/payments/pay_012.json,cust_X,41000,EUR,2024-07-04T10:02:00Z,pm_X,dev_X,Linz",
        "pay_013,/proc/payments/pay_013.json,cust_X,42500,EUR,2024-07-04T10:03:00Z,pm_X,dev_X,Brno",
        "pay_014,/proc/payments/pay_014.json,cust_X,39500,EUR,2024-07-04T10:04:00Z,pm_X,dev_X,Bratislava",
        # Unrelated normal payments
        "pay_020,/proc/payments/pay_020.json,cust_Y,3000,EUR,2024-07-05T11:00:00Z,pm_Y,dev_Y,Vienna",
    ]
    vm = FakeVM(sql_rows="\n".join([csv_header, *csv_rows]) + "\n")

    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_ids"] == [
        "pay_010",
        "pay_011",
        "pay_012",
        "pay_013",
        "pay_014",
    ]
    assert result["refs_to_submit"] == [
        "/proc/payments/pay_010.json",
        "/proc/payments/pay_011.json",
        "/proc/payments/pay_012.json",
        "/proc/payments/pay_013.json",
        "/proc/payments/pay_014.json",
    ]
    assert result["total_cents"] == 40000 + 42000 + 41000 + 42500 + 39500
    assert result["total_message"] == "EUR 2050.00"
    assert result["fraud_payment_count"] == 5
    assert result["selected_incident_count"] >= 1


def test_analyze_payment_fraud_history_returns_empty_on_empty_sql() -> None:
    # Schema is present but the table is empty: no warning, no fraud, no
    # spurious total_message (otherwise it would overwrite the archive total
    # inside the shared EvidenceLedger.fraud bucket).
    vm = FakeVM(sql_rows="payment_id\n")
    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_count"] == 0
    assert result["total_cents"] == 0
    assert result["total_message"] == ""
    assert result["refs_to_submit"] == []
    assert "warning" not in result


def test_analyze_payment_fraud_history_warns_when_schema_missing() -> None:
    vm = FakeVM(
        sql_rows="",
        schema_probe_stdout=SCHEMA_MISSING_PROBE_STDOUT,
    )
    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_count"] == 0
    assert result["total_message"] == ""
    assert "warning" in result
    assert "missing required tables" in result["warning"]
    # Heavy fraud SQL must NOT run when the probe already says the schema
    # cannot satisfy the query.
    assert all(
        "from payment_transactions" not in query for query in vm.queries
    )


def test_analyze_payment_fraud_history_falls_back_to_proc_path_when_record_path_missing() -> None:
    csv_header = (
        "payment_id,record_path,customer_id,payment_amount_cents,payment_currency,"
        "payment_created_at,payment_method_fingerprint,device_fingerprint,city"
    )
    csv_rows = [
        f"pay_{i:03d},,cust_X,40000,EUR,2024-07-04T10:0{i}:00Z,pm_X,dev_X,City{i}"
        for i in range(5)
    ]
    vm = FakeVM(sql_rows="\n".join([csv_header, *csv_rows]) + "\n")

    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    # When SQL omits record_path the helper must still emit a /proc/payments/<id>.json ref.
    assert result["refs_to_submit"] == [
        "/proc/payments/pay_000.json",
        "/proc/payments/pay_001.json",
        "/proc/payments/pay_002.json",
        "/proc/payments/pay_003.json",
        "/proc/payments/pay_004.json",
    ]


def test_analyze_payment_fraud_history_returns_warning_on_sql_failure() -> None:
    # Probe succeeds, but the heavy fraud SQL exits non-zero. The helper must
    # degrade to an empty result with an empty total_message so the agent can
    # fall back without stalling the trial.
    vm = FakeVM(sql_rows="", exit_code=1)
    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_count"] == 0
    assert result["refs_to_submit"] == []
    assert result["total_message"] == ""
    assert "warning" in result
    assert "fall back" in result["warning"].lower()
