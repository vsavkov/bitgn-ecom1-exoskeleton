from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import ExecRequest, ReadRequest

from receipt_price import (
    ReqAnalyzeReceiptPriceCheck,
    _money_to_cents,
    _sku_confusable_variants,
    analyze_receipt_price_check,
    analyze_receipt_price_content,
    parse_receipt_ocr,
)


RECEIPT_OCR = """ITEM QTY PRICE
FISCHER SX SX 7UY-G03.      1 EUR 23.00
  SKU/REF FST-69283OWE           UNIT 23.00
Gardena Smart PowerMax 28.  1 EUR 62.50
  SKU/REF GRD-36OWMOZT           UNIT 62.50
Bosch Bench IX0 3JP-J0U .   1 EUR 1207.00
  SKU/REF MAC-WEJK247H           UNIT 1207.00
BONDEX GARDEN CLASSIC I.    3 EUR 275.97
  SKU/REF PNT-3APVSF7J           UNIT 91.99
Subtotal                           EUR 1568.47
"""


TABLE_RECEIPT_OCR = """QTY  SKU                 DESCRIPTION        UNIT     TOTAL
 1   FST-69283OWE        FISCHER SX              23.00    23.00
 2   GRD-36OWMOZT        GARDENA SMART           62.50
Subtotal EUR                            148.00
"""


PRODUCT_ROWS = {
    "FST-69283OWE": {
        "product_sku": "FST-69283OWE",
        "product_name": "Fischer SX",
        "price_cents": "2400",
        "price_currency": "EUR",
        "record_path": "/proc/catalog/FST-69283OWE.json",
    },
    "GRD-360WMOZT": {
        "product_sku": "GRD-360WMOZT",
        "product_name": "Gardena Smart PowerMax",
        "price_cents": "6200",
        "price_currency": "EUR",
        "record_path": "/proc/catalog/GRD-360WMOZT.json",
    },
    "MAC-WEJK247H": {
        "product_sku": "MAC-WEJK247H",
        "product_name": "Bosch Bench",
        "price_cents": "120700",
        "price_currency": "EUR",
        "record_path": "/proc/catalog/MAC-WEJK247H.json",
    },
    "PNT-3APVSF7J": {
        "product_sku": "PNT-3APVSF7J",
        "product_name": "Bondex Garden Classic",
        "price_cents": "9200",
        "price_currency": "EUR",
        "record_path": "/proc/catalog/PNT-3APVSF7J.json",
    },
}


class FakeReceiptVM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.queries: list[str] = []

    def read(self, request: ReadRequest) -> SimpleNamespace:
        assert request.path == "/uploads/receipt_ocr.txt"
        return SimpleNamespace(content=self.content, truncated=False)

    def exec(self, request: ExecRequest) -> SimpleNamespace:
        assert request.path == "/bin/sql"
        self.queries.append(request.stdin)
        if "GRD-360WMOZT" in request.stdin:
            rows = [PRODUCT_ROWS["GRD-360WMOZT"]]
        else:
            rows = [
                PRODUCT_ROWS["FST-69283OWE"],
                PRODUCT_ROWS["MAC-WEJK247H"],
                PRODUCT_ROWS["PNT-3APVSF7J"],
            ]
        return SimpleNamespace(stdout=_csv(rows), stderr="", exit_code=0)


def _csv(rows: list[dict[str, str]]) -> str:
    header = "product_sku,product_name,price_cents,price_currency,record_path"
    return "\n".join(
        [
            header,
            *(
                ",".join(row[column] for column in header.split(","))
                for row in rows
            ),
        ]
    )


def test_money_to_cents_accepts_dot_and_comma() -> None:
    assert _money_to_cents("12.34") == 1234
    assert _money_to_cents("12,34") == 1234
    assert _money_to_cents("12") == 1200


def test_parse_receipt_ocr_extracts_subtotal_items_and_quantities() -> None:
    subtotal_cents, items = parse_receipt_ocr(RECEIPT_OCR)

    assert subtotal_cents == 156847
    assert [item.raw_sku for item in items] == [
        "FST-69283OWE",
        "GRD-36OWMOZT",
        "MAC-WEJK247H",
        "PNT-3APVSF7J",
    ]
    assert items[-1].quantity == 3
    assert items[-1].receipt_line_cents == 27597
    assert items[-1].receipt_unit_cents == 9199


def test_parse_receipt_ocr_extracts_table_rows_without_temp_rewrite() -> None:
    subtotal_cents, items = parse_receipt_ocr(TABLE_RECEIPT_OCR)

    assert subtotal_cents == 14800
    assert [item.raw_sku for item in items] == ["FST-69283OWE", "GRD-36OWMOZT"]
    assert [item.quantity for item in items] == [1, 2]
    assert items[1].receipt_unit_cents == 6250
    assert items[1].receipt_line_cents == 12500


def test_sku_confusable_variants_include_ocr_digit_repairs() -> None:
    variants = _sku_confusable_variants("GRD-36OWMOZT")

    assert variants[0] == "GRD-36OWMOZT"
    assert "GRD-360WMOZT" in variants


def test_analyze_receipt_price_content_compares_current_catalogue_subtotal() -> None:
    result = analyze_receipt_price_content(
        "/uploads/receipt_ocr.txt",
        RECEIPT_OCR,
        product_rows=PRODUCT_ROWS,
        resolved_skus={
            "FST-69283OWE": "FST-69283OWE",
            "GRD-36OWMOZT": "GRD-360WMOZT",
            "MAC-WEJK247H": "MAC-WEJK247H",
            "PNT-3APVSF7J": "PNT-3APVSF7J",
        },
    )

    assert result["formatted_message"] == "<YES>"
    assert result["receipt_subtotal_cents"] == 156847
    assert result["current_subtotal_cents"] == 156900
    assert result["difference_cents"] == 53
    assert result["refs_to_submit"] == [
        "/uploads/receipt_ocr.txt",
        "/proc/catalog/FST-69283OWE.json",
        "/proc/catalog/GRD-360WMOZT.json",
        "/proc/catalog/MAC-WEJK247H.json",
        "/proc/catalog/PNT-3APVSF7J.json",
    ]


def test_analyze_receipt_price_check_resolves_ocr_sku_confusion() -> None:
    vm = FakeReceiptVM(RECEIPT_OCR)

    result = analyze_receipt_price_check(
        vm,
        ReqAnalyzeReceiptPriceCheck(path="/uploads/receipt_ocr.txt"),
    )

    assert result["formatted_message"] == "<YES>"
    assert result["current_subtotal_cents"] == 156900
    assert result["within_tolerance"] is True
    assert "/proc/catalog/GRD-360WMOZT.json" in result["refs_to_submit"]
    assert len(vm.queries) == 2
