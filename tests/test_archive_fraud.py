from dataclasses import dataclass

from archive_fraud import (
    ReqAnalyzeArchiveFraudExport,
    _format_eur,
    _parse_archive_tsv,
    analyze_archive_fraud_content,
    analyze_archive_fraud_export,
    detect_archive_fraud,
)


HEADER = "\t".join(
    [
        "row_id",
        "archive_payment_id",
        "created_at",
        "customer_ref",
        "store_ref",
        "store_city",
        "amount_cents",
        "currency",
        "payment_method_fingerprint",
        "device_fingerprint",
        "observed_lat",
        "observed_lon",
        "sku_summary",
        "archive_channel",
    ]
)


@dataclass
class ReadResult:
    content: str
    truncated: bool = False


class FakeVM:
    def __init__(self, content: str) -> None:
        self.content = content

    def read(self, request) -> ReadResult:
        if request.path != "/archive/payments.tsv":
            raise AssertionError(f"unexpected read path: {request.path}")
        return ReadResult(content=self.content)


def tsv_row(
    row_id: str,
    created_at: str,
    customer: str,
    city: str,
    amount: int,
    payment: str,
    device: str,
    channel: str = "web",
) -> str:
    return "\t".join(
        [
            row_id,
            f"ap_{row_id}",
            created_at,
            customer,
            f"arch_store_{city.lower()}",
            city,
            str(amount),
            "EUR",
            payment,
            device,
            "0",
            "0",
            "SKU",
            channel,
        ]
    )


def sample_export() -> str:
    rapid_rows = [
        tsv_row("R1", "2023-11-12T08:34:18Z", "arch_cust_100", "Graz", 700, "pm_a", "dev_a", "mobile_app"),
        tsv_row("R2", "2023-11-12T08:34:40Z", "arch_cust_100", "Graz", 3500, "pm_b", "dev_b", "mobile_app"),
        tsv_row("R3", "2023-11-12T08:35:00Z", "arch_cust_100", "Salzburg", 9900, "pm_a", "dev_a", "mobile_app"),
        tsv_row("R4", "2023-11-12T08:35:14Z", "arch_cust_100", "Bratislava", 1700, "pm_a", "dev_a", "mobile_app"),
        tsv_row("R5", "2023-11-12T08:35:42Z", "arch_cust_100", "Salzburg", 10100, "pm_b", "dev_b", "mobile_app"),
        tsv_row("R6", "2023-11-12T08:36:18Z", "arch_cust_100", "Vienna", 2700, "pm_a", "dev_a", "mobile_app"),
        tsv_row("R7", "2023-11-12T08:36:33Z", "arch_cust_100", "Linz", 11700, "pm_b", "dev_b", "mobile_app"),
        tsv_row("R8", "2023-11-12T08:36:38Z", "arch_cust_100", "Brno", 4600, "pm_b", "dev_b", "mobile_app"),
        tsv_row("R9", "2023-11-12T08:36:42Z", "arch_cust_100", "Innsbruck", 9100, "pm_a", "dev_a", "mobile_app"),
        tsv_row("R10", "2023-11-12T08:38:03Z", "arch_cust_100", "Vienna", 1000, "pm_b", "dev_b", "mobile_app"),
    ]
    high_value_rows = [
        tsv_row("H1", "2023-10-17T03:21:30Z", "arch_cust_078", "Vienna", 72000, "pm_high", "dev_high"),
        tsv_row("H2", "2023-10-17T03:34:00Z", "arch_cust_078", "Graz", 36000, "pm_high", "dev_high"),
        tsv_row("H3", "2023-10-17T03:45:36Z", "arch_cust_078", "Graz", 214100, "pm_high", "dev_high"),
        tsv_row("H4", "2023-10-17T03:51:56Z", "arch_cust_078", "Salzburg", 237100, "pm_high", "dev_high"),
    ]
    normal_rows = [
        tsv_row("N1", "2023-10-01T03:21:30Z", "arch_cust_001", "Vienna", 72000, "pm_normal", "dev_normal"),
        tsv_row("N2", "2023-10-03T03:21:30Z", "arch_cust_001", "Graz", 72000, "pm_normal", "dev_normal"),
        tsv_row("N3", "2023-10-05T03:21:30Z", "arch_cust_001", "Salzburg", 72000, "pm_normal", "dev_normal"),
        tsv_row("N4", "2023-10-07T03:21:30Z", "arch_cust_001", "Brno", 72000, "pm_normal", "dev_normal"),
    ]
    return "\n".join([HEADER, *normal_rows, *rapid_rows, *high_value_rows]) + "\n"


def test_parse_archive_tsv_and_format_eur() -> None:
    rows = _parse_archive_tsv(
        "\n".join(
            [
                HEADER,
                tsv_row("R1", "2023-11-12T08:34:18Z", "cust", "Graz", 12345, "pm", "dev"),
            ]
        )
    )

    assert rows[0].row_id == "R1"
    assert rows[0].amount_cents == 12345
    assert _format_eur(12345) == "EUR 123.45"


def test_detect_archive_fraud_finds_bursts_and_ignores_slow_repeats() -> None:
    rows = _parse_archive_tsv(sample_export())
    fraud_rows, incidents = detect_archive_fraud(rows)

    assert [row.row_id for row in fraud_rows] == [
        "R1",
        "R2",
        "R3",
        "R4",
        "R5",
        "R6",
        "R7",
        "R8",
        "R9",
        "R10",
        "H1",
        "H2",
        "H3",
        "H4",
    ]
    assert "rapid_customer_multicity" in {incident.rule for incident in incidents}
    assert any(incident.rule.startswith("high_value_") for incident in incidents)


def test_detect_archive_fraud_selects_one_overlapping_window() -> None:
    cities = ["Graz", "Salzburg", "Bratislava", "Vienna", "Linz", "Brno", "Innsbruck"]
    rows = [
        tsv_row(
            f"O{index}",
            f"2023-11-12T08:{minute:02d}:{second:02d}Z",
            "arch_cust_overlap",
            cities[index % len(cities)],
            1000,
            "pm_overlap",
            "dev_overlap",
            "mobile_app",
        )
        for index, (minute, second) in enumerate(
            [
                (0, 0),
                (0, 26),
                (0, 52),
                (1, 18),
                (1, 44),
                (2, 10),
                (2, 36),
                (3, 2),
                (3, 28),
                (3, 54),
                (4, 20),
                (4, 46),
                (5, 12),
            ],
            start=1,
        )
    ]

    fraud_rows, incidents = detect_archive_fraud(
        _parse_archive_tsv("\n".join([HEADER, *rows]) + "\n")
    )

    assert len(incidents) == 1
    assert [row.row_id for row in fraud_rows] == [f"O{index}" for index in range(1, 13)]


def test_detect_archive_fraud_ignores_shared_service_desk_device() -> None:
    cities = ["Graz", "Salzburg", "Bratislava", "Vienna", "Linz", "Brno"]
    rows = [
        tsv_row(
            f"D{index}",
            f"2023-11-12T08:0{index}:00Z",
            f"arch_cust_{index:03d}",
            cities[index % len(cities)],
            1000,
            f"pm_{index}",
            "dev_shared_desk",
            "service_desk",
        )
        for index in range(6)
    ]

    fraud_rows, incidents = detect_archive_fraud(
        _parse_archive_tsv("\n".join([HEADER, *rows]) + "\n")
    )

    assert fraud_rows == []
    assert incidents == []


def test_detect_archive_fraud_ignores_service_desk_customer_without_reused_payment() -> None:
    cities = [
        "Graz",
        "Salzburg",
        "Bratislava",
        "Vienna",
        "Linz",
        "Brno",
        "Innsbruck",
    ]
    rows = [
        tsv_row(
            f"C{index}",
            f"2023-11-12T08:{index:02d}:00Z",
            "arch_cust_service_desk",
            cities[index % len(cities)],
            1000,
            f"pm_unique_{index}",
            "dev_shared_desk",
            "service_desk",
        )
        for index in range(7)
    ]

    fraud_rows, incidents = detect_archive_fraud(
        _parse_archive_tsv("\n".join([HEADER, *rows]) + "\n")
    )

    assert fraud_rows == []
    assert incidents == []


def test_detect_archive_fraud_keeps_service_desk_reused_payment_signal() -> None:
    cities = ["Graz", "Salzburg", "Bratislava", "Vienna", "Linz", "Brno"]
    rows = [
        tsv_row(
            f"P{index}",
            f"2023-11-12T08:0{index}:00Z",
            "arch_cust_service_desk",
            cities[index % len(cities)],
            1000,
            "pm_reused",
            "dev_shared_desk",
            "service_desk",
        )
        for index in range(6)
    ]

    fraud_rows, incidents = detect_archive_fraud(
        _parse_archive_tsv("\n".join([HEADER, *rows]) + "\n")
    )

    assert [row.row_id for row in fraud_rows] == [f"P{index}" for index in range(6)]
    assert {incident.rule for incident in incidents} == {"rapid_payment_multicity"}


def test_analyze_archive_fraud_content_returns_message_and_refs() -> None:
    result = analyze_archive_fraud_content("/archive/payments.tsv", sample_export())

    assert result["total_cents"] == 614_200
    assert result["total_message"] == "EUR 6142.00"
    assert result["fraud_row_count"] == 14
    assert result["candidate_incident_count"] >= result["selected_incident_count"]
    assert result["suppressed_overlapping_candidate_count"] >= 0
    assert "diagnostics" in result["incidents"][0]
    assert result["incidents"][0]["diagnostics"]["row_density"] > 0
    assert result["refs_to_submit"][0] == "/archive/payments.tsv#row=R1"
    assert result["refs_to_submit"][-1] == "/archive/payments.tsv#row=H4"


def test_analyze_archive_fraud_export_reads_runtime_file() -> None:
    result = analyze_archive_fraud_export(
        FakeVM(sample_export()),
        ReqAnalyzeArchiveFraudExport(path="/archive/payments.tsv"),
    )

    assert result["total_message"] == "EUR 6142.00"
