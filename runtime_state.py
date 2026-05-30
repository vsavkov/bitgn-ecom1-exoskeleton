import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ListRequest, NodeKind, ReadRequest, StatRequest
from connectrpc.errors import ConnectError

from runtime_calls import runtime_list, runtime_read, runtime_stat


class RuntimeVM(Protocol):
    def list(self, request: ListRequest) -> Any: ...

    def read(self, request: ReadRequest) -> Any: ...

    def stat(self, request: StatRequest) -> Any: ...


@dataclass(frozen=True)
class JsonRecord:
    path: str
    data: dict[str, Any]


CART_ROOTS = ("/proc/carts", "/proc/baskets")
PAYMENT_ROOTS = ("/proc/payment-ledger", "/proc/payments")
RETURN_ROOTS = ("/proc/return-workflows", "/proc/returns")
STORE_ROOTS = ("/proc/locations", "/proc/stores")
STAFF_ROOTS = ("/proc/staff", "/proc/employees")


def normalize_record_id(value: object) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def ids_equal(left: object, right: object) -> bool:
    return bool(left) and bool(right) and normalize_record_id(left) == normalize_record_id(right)


def id_variants(value: object) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    variants = [raw]
    hyphen = raw.replace("_", "-")
    underscore = raw.replace("-", "_")
    for candidate in (hyphen, underscore):
        if candidate not in variants:
            variants.append(candidate)
    return variants


def record_id(record: JsonRecord) -> str:
    value = record.data.get("id")
    if value:
        return str(value)
    name = PurePosixPath(record.path).name
    return name.removesuffix(".json")


def record_customer_id(record: JsonRecord) -> str:
    value = record.data.get("customer_id")
    if value:
        return str(value)
    parts = _path_parts(record.path)
    if len(parts) >= 4 and parts[0] == "proc" and parts[1] in {
        "carts",
        "payment-ledger",
        "return-workflows",
    }:
        return parts[2]
    return ""


def record_created_at(record: JsonRecord) -> str:
    for key in (
        "created_at",
        "updated_at",
        "basket_created_at",
        "payment_created_at",
        "return_created_at",
    ):
        value = record.data.get(key)
        if value:
            return str(value)
    return ""


def record_status(record: JsonRecord) -> str:
    for key in ("status", "basket_status", "payment_status", "return_status"):
        value = record.data.get(key)
        if value:
            return str(value)
    return ""


def record_amount_cents(record: JsonRecord) -> int | None:
    for key in ("amount_cents", "payment_amount_cents"):
        value = record.data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def record_currency(record: JsonRecord) -> str:
    for key in ("currency", "payment_currency"):
        value = record.data.get(key)
        if value:
            return str(value)
    return ""


def records_for_customer(
    vm: RuntimeVM,
    roots: Iterable[str],
    customer_id: str,
) -> list[JsonRecord]:
    records: list[JsonRecord] = []
    scanned_any_customer_dir = False
    for root in roots:
        for customer_variant in id_variants(customer_id):
            customer_root = f"{root.rstrip('/')}/{customer_variant}"
            customer_records = list(iter_json_records(vm, [customer_root]))
            if customer_records:
                scanned_any_customer_dir = True
                records.extend(customer_records)

    if not scanned_any_customer_dir:
        for record in iter_json_records(vm, roots):
            if ids_equal(record_customer_id(record), customer_id):
                records.append(record)

    return _dedupe_records(records)


def find_record_by_id(
    vm: RuntimeVM,
    roots: Iterable[str],
    wanted_id: str,
    *,
    customer_id: str | None = None,
) -> JsonRecord | None:
    if not wanted_id:
        return None

    candidates: list[JsonRecord] = []
    if customer_id:
        candidates.extend(records_for_customer(vm, roots, customer_id))
    else:
        candidates.extend(iter_json_records(vm, roots))

    for record in candidates:
        if ids_equal(record_id(record), wanted_id):
            return record
    return None


def iter_json_records(vm: RuntimeVM, roots: Iterable[str]) -> list[JsonRecord]:
    records: list[JsonRecord] = []
    seen: set[str] = set()
    for root in roots:
        records.extend(_iter_json_records_under(vm, root, seen))
    return _dedupe_records(records)


def read_json_record(vm: RuntimeVM, path: str) -> JsonRecord | None:
    try:
        result = runtime_read(
            vm,
            ReadRequest(path=path, number=False, start_line=0, end_line=0),
        )
    except (AttributeError, ConnectError):
        return None

    content = getattr(result, "content", "") or ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return JsonRecord(path=path, data=payload)


def try_stat(vm: RuntimeVM, path: str) -> bool:
    try:
        runtime_stat(vm, StatRequest(path=path))
        return True
    except (AttributeError, ConnectError):
        return False


def _iter_json_records_under(
    vm: RuntimeVM,
    root: str,
    seen: set[str],
) -> list[JsonRecord]:
    if root in seen:
        return []
    seen.add(root)

    try:
        listing = runtime_list(vm, ListRequest(path=root))
    except (AttributeError, ConnectError):
        record = read_json_record(vm, root)
        return [record] if record else []

    records: list[JsonRecord] = []
    for entry in getattr(listing, "entries", []) or []:
        path = getattr(entry, "path", "") or f"{root.rstrip('/')}/{entry.name}"
        kind = getattr(entry, "kind", NodeKind.NODE_KIND_UNSPECIFIED)
        if kind == NodeKind.NODE_KIND_DIR:
            records.extend(_iter_json_records_under(vm, path, seen))
            continue
        if kind not in {NodeKind.NODE_KIND_FILE, NodeKind.NODE_KIND_UNSPECIFIED}:
            continue
        if not path.endswith(".json"):
            continue
        record = read_json_record(vm, path)
        if record:
            records.append(record)
    return records


def _dedupe_records(records: Iterable[JsonRecord]) -> list[JsonRecord]:
    seen: set[str] = set()
    result: list[JsonRecord] = []
    for record in records:
        if record.path in seen:
            continue
        seen.add(record.path)
        result.append(record)
    return result


def _path_parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part]
