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
        sql_pages: list[str] | None = None,
        exit_code: int = 0,
        schema_probe_stdout: str = SCHEMA_OK_PROBE_STDOUT,
        schema_probe_exit: int = 0,
    ) -> None:
        self.sql_rows = sql_rows
        self.sql_pages = sql_pages
        self.sql_page_index = 0
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
        if self.sql_pages is not None:
            page_index = min(self.sql_page_index, len(self.sql_pages) - 1)
            self.sql_page_index += 1
            return ExecResult(stdout=self.sql_pages[page_index], exit_code=self.exit_code)
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


def test_analyze_payment_fraud_history_skips_malformed_rows() -> None:
    csv_header = (
        "payment_id,record_path,customer_id,payment_amount_cents,payment_currency,"
        "payment_created_at,payment_method_fingerprint,device_fingerprint,city"
    )
    csv_rows = [
        "pay_bad,/proc/payments/pay_bad.json,cust_bad,40000,EUR,not-a-date,pm_bad,dev_bad,Vienna",
        "pay_010,/proc/payments/pay_010.json,cust_X,40000,EUR,2024-07-04T10:00:00Z,pm_X,dev_X,Vienna",
        "pay_011,/proc/payments/pay_011.json,cust_X,42000,EUR,2024-07-04T10:01:00Z,pm_X,dev_X,Graz",
        "pay_012,/proc/payments/pay_012.json,cust_X,41000,EUR,2024-07-04T10:02:00Z,pm_X,dev_X,Linz",
        "pay_013,/proc/payments/pay_013.json,cust_X,42500,EUR,2024-07-04T10:03:00Z,pm_X,dev_X,Brno",
        "pay_014,/proc/payments/pay_014.json,cust_X,39500,EUR,2024-07-04T10:04:00Z,pm_X,dev_X,Bratislava",
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
    assert result["total_message"] == "EUR 2050.00"
    assert "warning" in result
    assert "skipped 1 malformed payment row" in result["warning"]
    assert "fall back" not in result["warning"].lower()


def test_analyze_payment_fraud_history_paginates_large_sql_results() -> None:
    csv_header = (
        "payment_id,record_path,customer_id,payment_amount_cents,payment_currency,"
        "payment_created_at,payment_method_fingerprint,device_fingerprint,city"
    )
    normal_rows = [
        (
            f"pay_normal_{i:03d},/proc/payments/pay_normal_{i:03d}.json,"
            f"cust_{i:03d},1000,EUR,2024-07-03T10:{i % 60:02d}:00Z,"
            f"pm_{i:03d},dev_{i:03d},Vienna"
        )
        for i in range(90)
    ]
    fraud_rows = [
        "pay_010,/proc/payments/pay_010.json,cust_X,40000,EUR,2024-07-04T10:00:00Z,pm_X,dev_X,Vienna",
        "pay_011,/proc/payments/pay_011.json,cust_X,42000,EUR,2024-07-04T10:01:00Z,pm_X,dev_X,Graz",
        "pay_012,/proc/payments/pay_012.json,cust_X,41000,EUR,2024-07-04T10:02:00Z,pm_X,dev_X,Linz",
        "pay_013,/proc/payments/pay_013.json,cust_X,42500,EUR,2024-07-04T10:03:00Z,pm_X,dev_X,Brno",
        "pay_014,/proc/payments/pay_014.json,cust_X,39500,EUR,2024-07-04T10:04:00Z,pm_X,dev_X,Bratislava",
    ]
    vm = FakeVM(
        sql_pages=[
            "\n".join([csv_header, *normal_rows]) + "\n",
            "\n".join([csv_header, *fraud_rows]) + "\n",
        ],
    )

    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_ids"] == [
        "pay_010",
        "pay_011",
        "pay_012",
        "pay_013",
        "pay_014",
    ]
    payment_queries = [
        query for query in vm.queries if "from payment_transactions" in query
    ]
    assert "limit 90 offset 0" in payment_queries[0]
    assert "limit 90 offset 90" in payment_queries[1]


def test_analyze_payment_fraud_history_detects_live_city_hop_chain() -> None:
    csv_header = (
        "payment_id,record_path,customer_id,payment_amount_cents,payment_currency,"
        "payment_created_at,payment_method_fingerprint,device_fingerprint,city"
    )
    csv_rows = [
        "pay_hop_1,/proc/payments/pay_hop_1.json,cust_HOP,1200,EUR,2024-07-04T10:00:00Z,pm_A,dev_HOP,Vienna",
        "pay_hop_2,/proc/payments/pay_hop_2.json,cust_HOP,1300,EUR,2024-07-04T10:08:00Z,pm_B,dev_HOP,Graz",
        "pay_hop_3,/proc/payments/pay_hop_3.json,cust_HOP,1400,EUR,2024-07-04T10:16:00Z,pm_A,dev_HOP,Linz",
        "pay_hop_4,/proc/payments/pay_hop_4.json,cust_HOP,1500,EUR,2024-07-04T10:24:00Z,pm_B,dev_HOP,Brno",
        "pay_slow_1,/proc/payments/pay_slow_1.json,cust_SLOW,1200,EUR,2024-07-04T11:00:00Z,pm_SLOW,dev_A,Vienna",
        "pay_slow_2,/proc/payments/pay_slow_2.json,cust_SLOW,1300,EUR,2024-07-04T11:30:00Z,pm_SLOW,dev_B,Graz",
    ]
    vm = FakeVM(sql_rows="\n".join([csv_header, *csv_rows]) + "\n")

    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_ids"] == [
        "pay_hop_1",
        "pay_hop_2",
        "pay_hop_3",
        "pay_hop_4",
    ]
    assert result["fraud_payment_count"] == 4
    assert {incident["rule"] for incident in result["incidents"]} == {
        "live_customer_city_hop"
    }


def test_analyze_payment_fraud_history_ignores_isolated_three_payment_city_hop() -> None:
    csv_header = (
        "payment_id,record_path,customer_id,payment_amount_cents,payment_currency,"
        "payment_created_at,payment_method_fingerprint,device_fingerprint,city"
    )
    csv_rows = [
        "pay_hop_1,/proc/payments/pay_hop_1.json,cust_HOP,1200,EUR,2024-07-04T10:00:00Z,pm_A,dev_HOP,Vienna",
        "pay_hop_2,/proc/payments/pay_hop_2.json,cust_HOP,1300,EUR,2024-07-04T10:08:00Z,pm_B,dev_HOP,Graz",
        "pay_hop_3,/proc/payments/pay_hop_3.json,cust_HOP,1400,EUR,2024-07-04T10:16:00Z,pm_A,dev_HOP,Linz",
    ]
    vm = FakeVM(sql_rows="\n".join([csv_header, *csv_rows]) + "\n")

    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_ids"] == []
    assert result["fraud_payment_count"] == 0


def test_analyze_payment_fraud_history_ignores_isolated_two_payment_city_hop() -> None:
    csv_header = (
        "payment_id,record_path,customer_id,payment_amount_cents,payment_currency,"
        "payment_created_at,payment_method_fingerprint,device_fingerprint,city"
    )
    csv_rows = [
        "pay_hop_1,/proc/payments/pay_hop_1.json,cust_HOP,1200,EUR,2024-07-04T10:00:00Z,pm_A,dev_HOP,Vienna",
        "pay_hop_2,/proc/payments/pay_hop_2.json,cust_HOP,1300,EUR,2024-07-04T10:08:00Z,pm_B,dev_HOP,Graz",
    ]
    vm = FakeVM(sql_rows="\n".join([csv_header, *csv_rows]) + "\n")

    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_ids"] == []
    assert result["fraud_payment_count"] == 0


def test_analyze_payment_fraud_history_keeps_batched_two_payment_city_hops() -> None:
    csv_header = (
        "payment_id,record_path,customer_id,payment_amount_cents,payment_currency,"
        "payment_created_at,payment_method_fingerprint,device_fingerprint,city"
    )
    csv_rows = [
        "pay_hop_1,/proc/payments/pay_hop_1.json,cust_HOP_1,1200,EUR,2024-07-04T10:00:00Z,pm_A,dev_HOP_1,Vienna",
        "pay_hop_2,/proc/payments/pay_hop_2.json,cust_HOP_1,1300,EUR,2024-07-04T10:08:00Z,pm_B,dev_HOP_1,Graz",
        "pay_hop_3,/proc/payments/pay_hop_3.json,cust_HOP_2,1400,EUR,2024-07-04T11:00:00Z,pm_C,dev_HOP_2,Linz",
        "pay_hop_4,/proc/payments/pay_hop_4.json,cust_HOP_2,1500,EUR,2024-07-04T11:08:00Z,pm_D,dev_HOP_2,Brno",
        "pay_hop_5,/proc/payments/pay_hop_5.json,cust_HOP_3,1600,EUR,2024-07-04T12:00:00Z,pm_E,dev_HOP_3,Bratislava",
        "pay_hop_6,/proc/payments/pay_hop_6.json,cust_HOP_3,1700,EUR,2024-07-04T12:08:00Z,pm_F,dev_HOP_3,Vienna",
    ]
    vm = FakeVM(sql_rows="\n".join([csv_header, *csv_rows]) + "\n")

    result = analyze_payment_fraud_history(vm, ReqAnalyzePaymentFraudHistory())

    assert result["fraud_payment_ids"] == [
        "pay_hop_1",
        "pay_hop_2",
        "pay_hop_3",
        "pay_hop_4",
        "pay_hop_5",
        "pay_hop_6",
    ]
    incident_rules = {incident["rule"] for incident in result["incidents"]}
    assert incident_rules == {"live_customer_city_hop"}


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
