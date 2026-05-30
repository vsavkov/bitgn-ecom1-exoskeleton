import csv
import io
import itertools
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest, ReadRequest
from connectrpc.errors import ConnectError
from pydantic import BaseModel, Field


class RuntimeVM(Protocol):
    def read(self, request: ReadRequest) -> Any: ...

    def exec(self, request: ExecRequest) -> Any: ...


class ReqAnalyzeReceiptPriceCheck(BaseModel):
    path: str = Field(
        description=(
            "Absolute path to the uploaded receipt OCR text file, for example "
            "/uploads/receipt_ocr_abc123.txt."
        )
    )
    tolerance_cents: int = Field(
        default=200,
        description="Allowed absolute ex-VAT total difference in cents.",
    )


@dataclass(frozen=True)
class ReceiptLineItem:
    raw_sku: str
    quantity: int
    receipt_unit_cents: int
    receipt_line_cents: int


SKU_LINE_RE = re.compile(
    r"\bSKU/REF\s+([A-Z0-9]{3}-[A-Z0-9]{4,})\b(?:.*?\bUNIT\s+([0-9]+(?:[.,][0-9]{2})?))?",
    re.IGNORECASE,
)
TABLE_SKU_LINE_RE = re.compile(
    r"^\s*(\d+)\s+([A-Z0-9]{3}-[A-Z0-9]{4,})\b(?P<rest>.*)$",
    re.IGNORECASE,
)
LINE_TOTAL_RE = re.compile(r"\b(\d+)\s+EUR\s+([0-9]+(?:[.,][0-9]{2})?)\b")
MONEY_RE = re.compile(r"\b([0-9]+(?:[.,][0-9]{2})?)\b")
SUBTOTAL_RE = re.compile(r"^\s*SUB\s*T[O0]TAL\b(?P<rest>.*)$", re.IGNORECASE)
# Receipt OCR often swaps visually similar characters inside SKU suffixes. Keep
# this narrow and require a unique catalogue match before accepting a repair.
CONFUSABLE_GROUPS = ("0O", "1IL", "2Z", "5S", "8B")


def _money_to_cents(value: str) -> int:
    try:
        amount = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise RuntimeError(f"invalid money amount: {value}") from exc
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_rows(vm: RuntimeVM, query: str) -> list[dict[str, str]]:
    try:
        result = vm.exec(ExecRequest(path="/bin/sql", stdin=query))
    except ConnectError as exc:
        raise RuntimeError(f"receipt price SQL query failed: {exc.message}") from exc

    if getattr(result, "exit_code", 0):
        raise RuntimeError(
            "receipt price SQL query exited with "
            f"{result.exit_code}: {(result.stderr or '').strip()}"
        )

    stdout = (result.stdout or "").strip()
    if not stdout:
        return []

    try:
        return [dict(row) for row in csv.DictReader(io.StringIO(stdout))]
    except csv.Error as exc:
        raise RuntimeError("receipt price SQL returned invalid CSV") from exc


def _parse_subtotal_cents(content: str) -> int:
    for line in content.splitlines():
        match = SUBTOTAL_RE.match(line)
        if not match:
            continue
        amounts = MONEY_RE.findall(match.group("rest"))
        if amounts:
            return _money_to_cents(amounts[-1])
    raise RuntimeError("receipt OCR does not contain a subtotal line")


def _line_quantity_and_total(lines: list[str], sku_line_index: int, unit_cents: int) -> tuple[int, int]:
    # The item summary line normally precedes its SKU/REF line, so scan a small
    # local window instead of treating every EUR amount on the receipt as a line.
    for line in reversed(lines[max(0, sku_line_index - 3) : sku_line_index]):
        matches = LINE_TOTAL_RE.findall(line)
        if not matches:
            continue
        quantity_text, total_text = matches[-1]
        quantity = int(quantity_text)
        return quantity, _money_to_cents(total_text)

    if unit_cents <= 0:
        raise RuntimeError("receipt OCR line is missing quantity and unit price")
    return 1, unit_cents


def _table_line_prices(rest: str, quantity: int) -> tuple[int, int]:
    money_values = [_money_to_cents(value) for value in MONEY_RE.findall(rest)]
    if not money_values:
        return 0, 0
    if len(money_values) >= 2:
        return money_values[-2], money_values[-1]
    unit_cents = money_values[-1]
    return unit_cents, unit_cents * quantity


def parse_receipt_ocr(content: str) -> tuple[int, list[ReceiptLineItem]]:
    lines = content.splitlines()
    subtotal_cents = _parse_subtotal_cents(content)
    items: list[ReceiptLineItem] = []

    for index, line in enumerate(lines):
        table_match = TABLE_SKU_LINE_RE.match(line)
        if table_match:
            quantity = int(table_match.group(1))
            raw_sku = table_match.group(2).upper()
            unit_cents, line_cents = _table_line_prices(
                table_match.group("rest"),
                quantity,
            )
            items.append(
                ReceiptLineItem(
                    raw_sku=raw_sku,
                    quantity=quantity,
                    receipt_unit_cents=unit_cents,
                    receipt_line_cents=line_cents,
                )
            )
            continue

        match = SKU_LINE_RE.search(line)
        if not match:
            continue
        raw_sku = match.group(1).upper()
        unit_cents = _money_to_cents(match.group(2)) if match.group(2) else 0
        quantity, line_cents = _line_quantity_and_total(lines, index, unit_cents)
        if not unit_cents and quantity:
            unit_cents = round(line_cents / quantity)
        items.append(
            ReceiptLineItem(
                raw_sku=raw_sku,
                quantity=quantity,
                receipt_unit_cents=unit_cents,
                receipt_line_cents=line_cents,
            )
        )

    if not items:
        raise RuntimeError("receipt OCR does not contain SKU/REF lines")
    return subtotal_cents, items


def _sku_confusable_variants(sku: str, *, limit: int = 128) -> list[str]:
    if "-" not in sku:
        return [sku]

    prefix, suffix = sku.split("-", 1)
    replacements: list[list[str]] = []
    for char in suffix:
        group = next((group for group in CONFUSABLE_GROUPS if char in group), "")
        replacements.append(list(group) if group else [char])

    variants = [
        f"{prefix}-{''.join(chars)}"
        for chars in itertools.product(*replacements)
        if f"{prefix}-{''.join(chars)}" != sku
    ]
    return [sku, *variants[: limit - 1]]


def _fetch_current_products(
    vm: RuntimeVM,
    skus: list[str],
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    if not skus:
        return {}, {}

    exact_rows = _product_rows_for_skus(vm, skus)
    rows_by_sku = {row["product_sku"]: row for row in exact_rows if row.get("product_sku")}
    resolved_skus = {sku: sku for sku in skus if sku in rows_by_sku}

    missing_skus = [sku for sku in skus if sku not in resolved_skus]
    for sku in missing_skus:
        variants = _sku_confusable_variants(sku)
        variant_rows = _product_rows_for_skus(vm, variants)
        # Ambiguous OCR repairs are worse than a hard failure: they can flip a
        # yes/no receipt decision. Only accept a correction with one target row.
        if len(variant_rows) != 1:
            continue
        resolved = variant_rows[0].get("product_sku") or ""
        if resolved:
            rows_by_sku[resolved] = variant_rows[0]
            resolved_skus[sku] = resolved

    return rows_by_sku, resolved_skus


def _product_rows_for_skus(vm: RuntimeVM, skus: list[str]) -> list[dict[str, str]]:
    quoted = ", ".join(_sql_quote(sku) for sku in sorted(set(skus)))
    if not quoted:
        return []
    return _sql_rows(
        vm,
        "select product_sku, product_name, price_cents, price_currency, record_path "
        "from product_variants "
        f"where product_sku in ({quoted}) "
        "order by product_sku;",
    )


def analyze_receipt_price_content(
    path: str,
    content: str,
    *,
    product_rows: dict[str, dict[str, str]],
    resolved_skus: dict[str, str],
    tolerance_cents: int = 200,
) -> dict[str, Any]:
    receipt_subtotal_cents, items = parse_receipt_ocr(content)
    missing = [item.raw_sku for item in items if item.raw_sku not in resolved_skus]
    if missing:
        raise RuntimeError(f"receipt SKUs not found in current catalogue: {', '.join(missing)}")

    current_subtotal_cents = 0
    item_results: list[dict[str, Any]] = []
    refs = [path]
    for item in items:
        resolved_sku = resolved_skus[item.raw_sku]
        product = product_rows[resolved_sku]
        price_cents = int(product.get("price_cents") or "0")
        # Receipt checks compare the historical subtotal against current unit
        # prices for the same quantities; OCR line prices are evidence only.
        line_current_cents = item.quantity * price_cents
        current_subtotal_cents += line_current_cents
        refs.append(product.get("record_path") or "")
        item_results.append(
            {
                "raw_sku": item.raw_sku,
                "resolved_sku": resolved_sku,
                "quantity": item.quantity,
                "receipt_unit_cents": item.receipt_unit_cents,
                "current_unit_cents": price_cents,
                "current_line_cents": line_current_cents,
                "record_path": product.get("record_path") or "",
            }
        )

    difference_cents = abs(current_subtotal_cents - receipt_subtotal_cents)
    within_tolerance = difference_cents <= tolerance_cents
    return {
        "receipt_subtotal_cents": receipt_subtotal_cents,
        "current_subtotal_cents": current_subtotal_cents,
        "difference_cents": difference_cents,
        "tolerance_cents": tolerance_cents,
        "within_tolerance": within_tolerance,
        "formatted_message": "<YES>" if within_tolerance else "<NO>",
        "refs_to_submit": [ref for ref in dict.fromkeys(refs) if ref],
        "items": item_results,
    }


def analyze_receipt_price_check(
    vm: RuntimeVM,
    cmd: ReqAnalyzeReceiptPriceCheck,
) -> dict[str, Any]:
    try:
        result = vm.read(ReadRequest(path=cmd.path, number=False, start_line=0, end_line=0))
    except ConnectError as exc:
        raise RuntimeError(f"receipt OCR read failed: {exc.message}") from exc

    if getattr(result, "truncated", False):
        raise RuntimeError(f"receipt OCR is too large to read fully: {cmd.path}")

    content = result.content or ""
    _subtotal, items = parse_receipt_ocr(content)
    product_rows, resolved_skus = _fetch_current_products(
        vm,
        [item.raw_sku for item in items],
    )
    return analyze_receipt_price_content(
        cmd.path,
        content,
        product_rows=product_rows,
        resolved_skus=resolved_skus,
        tolerance_cents=cmd.tolerance_cents,
    )
