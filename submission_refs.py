import csv
import io
import json
import re
from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest, ListRequest, NodeKind, ReadRequest, StatRequest
from connectrpc.errors import ConnectError

from runtime_calls import runtime_exec, runtime_list, runtime_stat
from runtime_state import (
    CART_ROOTS,
    PAYMENT_ROOTS,
    RETURN_ROOTS,
    STAFF_ROOTS,
    STORE_ROOTS,
    find_record_by_id,
    read_json_record,
    record_customer_id,
)


class CompletionLike(Protocol):
    @property
    def task_type(self) -> str: ...

    @property
    def message(self) -> str: ...

    @property
    def protected_record_denial(self) -> bool: ...

    @property
    def grounding_doc_refs(self) -> Sequence[str]: ...

    @property
    def grounding_row_refs(self) -> Sequence[str]: ...


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...

    def list(self, request: ListRequest) -> Any: ...

    def read(self, request: ReadRequest) -> Any: ...

    def stat(self, request: StatRequest) -> Any: ...


class ExplicitRecordSpec(NamedTuple):
    table: str
    key_column: str
    canonical_prefix: str
    customer_column: str
    pattern: re.Pattern[str]


PRODUCT_SKU_RE = re.compile(
    r"^(?=[A-Z0-9-]{10,}$)[A-Z]{2,}(?:-[A-Z0-9]+)+$"
)
MESSAGE_SKU_RE = re.compile(
    r"(?<![A-Z0-9-])"
    r"(?=[A-Z0-9-]{10,}(?![A-Z0-9-]))"
    r"[A-Z]{2,}(?:-[A-Z0-9]+)+"
    r"(?![A-Z0-9-])"
)
UPLOAD_REF_RE = re.compile(r"^/uploads/.+", re.IGNORECASE)
CUSTOMER_ID_RE = re.compile(r"^cust[-_]\d+$")
EMPLOYEE_ID_RE = re.compile(r"^emp[-_]\d+$")
CROSS_CUSTOMER_DENIAL_RE = re.compile(
    r"\b(?:"
    r"another customer|other customer|different customer|"
    r"someone else's|someone else|not your|not yours|"
    r"does not belong to you|doesn't belong to you|not belong to you|"
    r"belongs? to (?:another|a different|other) customer|"
    r"owned by (?:another|a different|other) customer|"
    r"customer mismatch|cross[-\s]?customer"
    r")\b",
    re.IGNORECASE,
)
CUSTOMER_SCOPED_REF_RE = re.compile(
    r"^/proc/(?:baskets|customers|payments|returns|carts|payment-ledger|return-workflows)/",
    re.IGNORECASE,
)
CATALOG_REF_RE = re.compile(r"^/proc/catalog(?:/|$)", re.IGNORECASE)
BASKET_REF_RE = re.compile(
    r"^/proc/(?:baskets/|carts/[^/]+/)(?P<basket>basket[-_]\d+)(?:\.json)?$",
    re.IGNORECASE,
)
PROC_RECORD_TABLES: dict[str, tuple[str, str, re.Pattern[str]]] = {
    "baskets": ("shopping_baskets", "basket_id", re.compile(r"^basket_\d+$")),
    "customers": ("customer_accounts", "customer_id", CUSTOMER_ID_RE),
    "employees": ("employee_accounts", "employee_id", re.compile(r"^emp_\d+$")),
    "payments": ("payment_transactions", "payment_id", re.compile(r"^pay_\d+$")),
    "returns": ("return_requests", "return_id", re.compile(r"^ret_\d+$")),
    "stores": ("stores", "store_id", re.compile(r"^store_[a-z0-9_]+$")),
}

PROC_RECORD_ROOTS: dict[str, tuple[str, ...]] = {
    "baskets": CART_ROOTS,
    "carts": CART_ROOTS,
    "payments": PAYMENT_ROOTS,
    "payment-ledger": PAYMENT_ROOTS,
    "returns": RETURN_ROOTS,
    "return-workflows": RETURN_ROOTS,
    "stores": STORE_ROOTS,
    "locations": STORE_ROOTS,
    "employees": STAFF_ROOTS,
    "staff": STAFF_ROOTS,
}

EXPLICIT_RECORD_SPECS: tuple[ExplicitRecordSpec, ...] = (
    # User prompts are not perfectly consistent about record id spelling. Keep
    # alias extraction here, then resolve to canonical record_path through SQL.
    ExplicitRecordSpec(
        table="shopping_baskets",
        key_column="basket_id",
        canonical_prefix="basket",
        customer_column="customer_id",
        pattern=re.compile(
            r"(?<![A-Za-z0-9_])(?:baskets?|bask)[_-]?(\d+)(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    ),
    ExplicitRecordSpec(
        table="payment_transactions",
        key_column="payment_id",
        canonical_prefix="pay",
        customer_column="customer_id",
        pattern=re.compile(
            r"(?<![A-Za-z0-9_])(?:payments?|pay)[_-]?(\d+)(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    ),
    ExplicitRecordSpec(
        table="return_requests",
        key_column="return_id",
        canonical_prefix="ret",
        customer_column="customer_id",
        pattern=re.compile(
            r"(?<![A-Za-z0-9_])(?:returns?|ret)[_-]?(\d+)(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    ),
    ExplicitRecordSpec(
        table="customer_accounts",
        key_column="customer_id",
        canonical_prefix="cust",
        customer_column="customer_id",
        pattern=re.compile(
            r"(?<![A-Za-z0-9_])(?:customers?|cust)[_-]?(\d+)(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    ),
)


def dedupe_refs(refs: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ref in refs:
        ref = ref.strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        result.append(ref)
    return result


def is_document_ref(ref: str) -> bool:
    return ref.endswith(".md")


def is_runtime_navigation_doc_ref(ref: str) -> bool:
    path, _fragment = split_ref_fragment(ref)
    normalized = normalize_runtime_path(path).lower()
    return normalized.endswith("/readme.md") and normalized.startswith(
        ("/proc/", "/run/")
    )


def is_catalog_ref(ref: str) -> bool:
    path, _fragment = split_ref_fragment(ref)
    return bool(CATALOG_REF_RE.match(normalize_runtime_path(path)))


def normalize_runtime_path(path: str) -> str:
    if path == "/":
        return "/"
    return f"/{path.strip('/')}"


def try_stat(vm: RuntimeVM, path: str) -> bool:
    try:
        runtime_stat(vm, StatRequest(path=path))
        return True
    except ConnectError:
        return False


def canonical_case_file_ref(vm: RuntimeVM, path: str) -> str | None:
    normalized = normalize_runtime_path(path)
    parent, sep, filename = normalized.rpartition("/")
    if not sep or not parent:
        parent = "/"
    if not filename:
        return normalized if try_stat(vm, normalized) else None

    try:
        result = runtime_list(vm, ListRequest(path=parent))
    except (AttributeError, ConnectError):
        return normalized if try_stat(vm, normalized) else None

    exact_match: str | None = None
    case_match: str | None = None
    for entry in getattr(result, "entries", []) or []:
        if getattr(entry, "kind", None) not in {
            NodeKind.NODE_KIND_FILE,
            NodeKind.NODE_KIND_UNSPECIFIED,
        }:
            continue
        entry_name = getattr(entry, "name", "")
        if entry_name == filename:
            exact_match = entry_name
            break
        if entry_name.lower() == filename.lower():
            case_match = entry_name

    matched_name = exact_match or case_match
    if matched_name:
        canonical = f"{parent.rstrip('/')}/{matched_name}"
        return canonical if try_stat(vm, canonical) else None

    return normalized if try_stat(vm, normalized) else None


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_rows(vm: RuntimeVM, query: str) -> list[dict[str, str]]:
    try:
        result = runtime_exec(vm, ExecRequest(path="/bin/sql", stdin=query))
    except ConnectError:
        return []

    if getattr(result, "exit_code", 0):
        return []

    stdout = (result.stdout or "").strip()
    if not stdout:
        return []

    try:
        return [dict(row) for row in csv.DictReader(io.StringIO(stdout))]
    except csv.Error:
        return []


def sql_record_path(
    vm: RuntimeVM,
    *,
    table: str,
    key_column: str,
    value: str,
) -> str | None:
    rows = sql_rows(
        vm,
        f"select record_path from {table} "
        f"where {key_column} = {sql_quote(value)} limit 1;",
    )
    if not rows:
        return None
    path = rows[0].get("record_path") or ""
    return path if path.startswith("/") else None


def split_ref_fragment(ref: str) -> tuple[str, str]:
    if "#" not in ref:
        return ref, ""
    path, fragment = ref.split("#", 1)
    return path, f"#{fragment}"


def canonical_proc_record_ref(vm: RuntimeVM, path: str) -> str | None:
    normalized = normalize_runtime_path(path)
    if try_stat(vm, normalized):
        return normalized

    if not normalized.endswith(".json") and try_stat(vm, f"{normalized}.json"):
        return f"{normalized}.json"

    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 3 or parts[0] != "proc":
        return None

    name = parts[-1]
    if name.endswith(".json"):
        name = name.removesuffix(".json")

    if parts[1] == "catalog" or PRODUCT_SKU_RE.match(name):
        path_from_sql = sql_record_path(
            vm,
            table="product_variants",
            key_column="product_sku",
            value=name,
        )
        if path_from_sql and try_stat(vm, path_from_sql):
            return path_from_sql
        return None

    roots = PROC_RECORD_ROOTS.get(parts[1])
    if roots:
        record = find_record_by_id(vm, roots, name)
        if record and try_stat(vm, record.path):
            return record.path

    table_spec = PROC_RECORD_TABLES.get(parts[1])
    if table_spec is None:
        return None

    table, key_column, pattern = table_spec
    if not pattern.match(name):
        return None

    path_from_sql = sql_record_path(
        vm,
        table=table,
        key_column=key_column,
        value=name,
    )
    if path_from_sql and try_stat(vm, path_from_sql):
        return path_from_sql
    return None


def normalize_submission_refs(
    vm: RuntimeVM,
    refs: list[str],
) -> list[str]:
    normalized_refs: list[str] = []
    for ref in refs:
        path, fragment = split_ref_fragment(ref)
        normalized_path = normalize_runtime_path(path)

        if normalized_path.startswith("/archive/") and fragment:
            normalized_refs.append(f"{normalized_path}{fragment}")
            continue

        if is_document_ref(normalized_path):
            normalized_refs.append(normalized_path)
            continue

        if not normalized_path.startswith("/proc/"):
            canonical = canonical_case_file_ref(vm, normalized_path)
            if canonical:
                normalized_refs.append(f"{canonical}{fragment}")
            continue

        canonical = canonical_proc_record_ref(vm, normalized_path)
        if canonical:
            normalized_refs.append(f"{canonical}{fragment}")

    return dedupe_refs(normalized_refs)


def message_sku_refs(vm: RuntimeVM, message: str) -> list[str]:
    if not message:
        return []

    skus = sorted({sku for sku in MESSAGE_SKU_RE.findall(message)})
    if not skus:
        return []

    sku_values = ", ".join(sql_quote(sku) for sku in skus)
    rows = sql_rows(
        vm,
        "select record_path from product_variants "
        f"where product_sku in ({sku_values});",
    )
    return dedupe_refs(
        [
            row.get("record_path") or ""
            for row in rows
            if (row.get("record_path") or "").startswith("/")
        ]
    )


def completion_step_sku_refs(vm: RuntimeVM, cmd: CompletionLike) -> list[str]:
    steps = getattr(cmd, "completed_steps_laconic", None)
    if not isinstance(steps, Sequence) or isinstance(steps, str):
        return []

    sku_values: list[str] = []
    for step in steps:
        if isinstance(step, str):
            sku_values.extend(MESSAGE_SKU_RE.findall(step))
    return sku_refs(vm, sku_values)


def _catalog_record_ref_by_sku_from_tree(vm: RuntimeVM, sku: str) -> str | None:
    stack = ["/proc/catalog"]
    target = f"{sku}.json"
    while stack:
        root = stack.pop()
        try:
            listing = runtime_list(vm, ListRequest(path=root))
        except ConnectError:
            continue
        for entry in getattr(listing, "entries", []) or []:
            name = getattr(entry, "name", "")
            kind = getattr(entry, "kind", None)
            path = f"{root.rstrip('/')}/{name}"
            if kind == NodeKind.NODE_KIND_DIR:
                stack.append(path)
                continue
            if name == target and try_stat(vm, path):
                return path
    return None


def catalog_record_ref_by_sku(vm: RuntimeVM, sku: str) -> str | None:
    path_from_sql = sql_record_path(
        vm,
        table="product_variants",
        key_column="product_sku",
        value=sku,
    )
    if path_from_sql and try_stat(vm, path_from_sql):
        return path_from_sql
    return _catalog_record_ref_by_sku_from_tree(vm, sku)


def sku_refs(vm: RuntimeVM, skus: Sequence[str]) -> list[str]:
    refs: list[str] = []
    for sku in dict.fromkeys(skus):
        ref = catalog_record_ref_by_sku(vm, sku)
        if ref:
            refs.append(ref)
    return dedupe_refs(refs)


def task_sku_refs(vm: RuntimeVM, task_text: str) -> list[str]:
    if not task_text:
        return []
    return sku_refs(vm, MESSAGE_SKU_RE.findall(task_text))


def negated_task_sku_refs(vm: RuntimeVM, task_text: str) -> list[str]:
    if not task_text:
        return []

    negated_skus: list[str] = []
    for match in MESSAGE_SKU_RE.finditer(task_text):
        context = task_text[max(0, match.start() - 48) : match.start()].lower()
        if any(
            marker in context
            for marker in (
                "but not",
                "except",
                "excluding",
                "exclude",
                "not sku",
                "without sku",
            )
        ):
            negated_skus.append(match.group(0))
    return sku_refs(vm, negated_skus)


def upload_receipt_sku_refs(vm: RuntimeVM, refs: Sequence[str]) -> list[str]:
    sku_values: list[str] = []
    parsed_record_refs: list[str] = []
    for ref in refs:
        path, _fragment = split_ref_fragment(ref)
        path = normalize_runtime_path(path)
        if not UPLOAD_REF_RE.match(path):
            continue
        try:
            record = vm.read(ReadRequest(path=path, number=False, start_line=0, end_line=0))
        except (AttributeError, ConnectError):
            continue
        content = getattr(record, "content", "") or ""
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(decoded, dict) and isinstance(decoded.get("text"), str):
                content = decoded["text"]
        try:
            from receipt_price import _fetch_current_products, parse_receipt_ocr

            _subtotal, items = parse_receipt_ocr(content)
            product_rows, resolved_skus = _fetch_current_products(
                vm,
                [item.raw_sku for item in items],
            )
            for item in items:
                resolved_sku = resolved_skus.get(item.raw_sku)
                if not resolved_sku:
                    continue
                record_path = product_rows.get(resolved_sku, {}).get("record_path") or ""
                if record_path:
                    parsed_record_refs.append(record_path)
        except RuntimeError:
            pass
        sku_values.extend(MESSAGE_SKU_RE.findall(content))
    return dedupe_refs([*parsed_record_refs, *sku_refs(vm, sku_values)])


def availability_count_refs_from_catalog_result(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []

    refs: list[str] = []
    store_ref = result.get("store_ref")
    if isinstance(store_ref, str) and store_ref:
        refs.append(store_ref)

    result_refs = result.get("refs_to_submit_for_availability_count")
    if isinstance(result_refs, list):
        refs.extend(ref for ref in result_refs if isinstance(ref, str) and ref)

    return dedupe_refs(refs)


def availability_lookup_refs_from_catalog_result(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []

    refs: list[str] = []
    store_ref = result.get("store_ref")
    if isinstance(store_ref, str) and store_ref:
        refs.append(store_ref)

    items = result.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            item_refs = item.get("matched_refs")
            if isinstance(item_refs, list):
                refs.extend(ref for ref in item_refs if isinstance(ref, str) and ref)

    return dedupe_refs(refs)


def catalog_lookup_refs_from_catalog_result(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []

    refs: list[str] = []
    store_ref = result.get("store_ref")
    if isinstance(store_ref, str) and store_ref:
        refs.append(store_ref)

    items = result.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            item_refs = item.get("matched_refs")
            if isinstance(item_refs, list):
                refs.extend(ref for ref in item_refs if isinstance(ref, str) and ref)

    return dedupe_refs(refs)


def support_note_refs_from_catalog_result(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []

    refs: list[str] = []
    items = result.get("items")
    if not isinstance(items, list):
        return []

    for item in items:
        if not isinstance(item, dict):
            continue
        support_note = item.get("support_note_extra_claim")
        if not isinstance(support_note, dict):
            continue
        item_refs = support_note.get("refs_to_submit")
        if isinstance(item_refs, list):
            refs.extend(ref for ref in item_refs if isinstance(ref, str) and ref)

    return dedupe_refs(refs)


def catalog_refs_from_refs(refs: Sequence[str]) -> list[str]:
    return dedupe_refs([ref for ref in refs if is_catalog_ref(ref)])


def candidate_record_ids(prefix: str, numeric_text: str) -> list[str]:
    candidates = [f"{prefix}_{numeric_text}"]
    try:
        padded = f"{prefix}_{int(numeric_text):03d}"
    except ValueError:
        return candidates
    if padded not in candidates:
        candidates.append(padded)
    return candidates


def parse_runtime_identity(stdout: str) -> tuple[str | None, set[str]]:
    user_id: str | None = None
    roles: set[str] = set()
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key.strip() == "user":
            user_id = value.strip() or None
        elif key.strip() == "roles":
            roles.update(role.lower() for role in re.findall(r"[A-Za-z0-9_]+", value))
    return user_id, roles


def is_customer_identity(user_id: str | None) -> bool:
    return bool(user_id) and bool(CUSTOMER_ID_RE.match(user_id))


def runtime_identity(vm: RuntimeVM) -> tuple[str | None, set[str]]:
    try:
        result = runtime_exec(vm, ExecRequest(path="/bin/id"))
    except ConnectError:
        return None, set()
    return parse_runtime_identity(result.stdout or "")


def can_auto_cite_customer_scoped_record(
    *,
    user_id: str | None,
    roles: set[str],
    record_customer_id: str,
) -> bool:
    if not user_id:
        return False
    if is_customer_identity(user_id):
        return record_customer_id == user_id
    if roles <= {"guest"}:
        return False
    return True


def customer_scoped_ref_owner(
    vm: RuntimeVM,
    ref: str,
) -> str | None:
    path = normalize_runtime_path(split_ref_fragment(ref)[0])
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[0] != "proc":
        return None

    folder = parts[1]
    record_id = parts[2].removesuffix(".json")
    if folder == "customers" and CUSTOMER_ID_RE.match(record_id):
        return record_id

    if (
        folder in {"carts", "payment-ledger", "return-workflows"}
        and len(parts) >= 4
        and CUSTOMER_ID_RE.match(parts[2])
    ):
        return parts[2]

    record = read_json_record(vm, path)
    if record is None and not path.endswith(".json"):
        record = read_json_record(vm, f"{path}.json")
    if record is not None:
        owner = record_customer_id(record)
        if owner:
            return owner

    spec_by_folder = {
        "baskets": ("shopping_baskets", "basket_id"),
        "payments": ("payment_transactions", "payment_id"),
        "returns": ("return_requests", "return_id"),
    }
    spec = spec_by_folder.get(folder)
    if spec is None:
        return None

    table, key_column = spec
    try:
        rows = sql_rows(
            vm,
            "select customer_id "
            f"from {table} "
            f"where {key_column} = {sql_quote(record_id)} limit 1;",
        )
    except Exception:
        return None

    if not rows:
        return None
    return rows[0].get("customer_id") or None


def has_cross_customer_denial_ref(
    vm: RuntimeVM,
    row_refs: Sequence[str],
    *,
    user_id: str | None,
) -> bool:
    if not is_customer_identity(user_id):
        return False

    for ref in row_refs:
        path = normalize_runtime_path(split_ref_fragment(ref)[0])
        if not CUSTOMER_SCOPED_REF_RE.match(path):
            continue
        owner = customer_scoped_ref_owner(vm, ref)
        # Unknown ownership is not enough to discard evidence. A known
        # cross-customer owner in a security denial is protected record leakage.
        if owner is not None and owner != user_id:
            return True
    return False


def explicit_record_path_if_allowed(
    vm: RuntimeVM,
    spec: ExplicitRecordSpec,
    record_id: str,
    *,
    user_id: str | None,
    roles: set[str],
) -> str | None:
    roots = _roots_for_explicit_record_spec(spec)
    if roots:
        record = find_record_by_id(
            vm,
            roots,
            record_id,
            customer_id=user_id if is_customer_identity(user_id) else None,
        )
        if record is not None:
            owner = record_customer_id(record)
            if can_auto_cite_customer_scoped_record(
                user_id=user_id,
                roles=roles,
                record_customer_id=owner,
            ):
                return record.path

    rows = sql_rows(
        vm,
        f"select record_path, {spec.customer_column} as customer_id "
        f"from {spec.table} "
        f"where {spec.key_column} = {sql_quote(record_id)} limit 1;",
    )
    if not rows:
        return None

    path = rows[0].get("record_path") or ""
    if not path.startswith("/"):
        return None

    # Customer-scoped target refs are safe to auto-add only after an identity
    # check: customers may cite their own records, while employee/manager roles
    # may cite records in the operational workflow they are deciding.
    if not can_auto_cite_customer_scoped_record(
        user_id=user_id,
        roles=roles,
        record_customer_id=rows[0].get("customer_id") or "",
    ):
        return None

    return path


def _roots_for_explicit_record_spec(spec: ExplicitRecordSpec) -> tuple[str, ...]:
    if spec.table == "shopping_baskets":
        return CART_ROOTS
    if spec.table == "payment_transactions":
        return PAYMENT_ROOTS
    if spec.table == "return_requests":
        return RETURN_ROOTS
    return ()


def explicit_target_refs_from_task(
    vm: RuntimeVM,
    task_text: str,
    *,
    user_id: str | None = None,
    roles: set[str] | None = None,
) -> list[str]:
    candidates: list[tuple[ExplicitRecordSpec, str]] = []
    seen: set[tuple[str, str]] = set()
    for spec in EXPLICIT_RECORD_SPECS:
        for match in spec.pattern.finditer(task_text):
            for record_id in candidate_record_ids(spec.canonical_prefix, match.group(1)):
                key = (spec.table, record_id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((spec, record_id))

    if not candidates:
        return []

    if roles is None:
        roles = set()
    if user_id is None and not roles:
        user_id, roles = runtime_identity(vm)

    refs: list[str] = []
    for spec, record_id in candidates:
        path = explicit_record_path_if_allowed(
            vm,
            spec,
            record_id,
            user_id=user_id,
            roles=roles,
        )
        if path:
            refs.append(path)
    return dedupe_refs(refs)


def is_customer_or_guest_context(user_id: str | None, roles: set[str]) -> bool:
    if is_customer_identity(user_id):
        return True
    normalized_user = (user_id or "").lower()
    if normalized_user.startswith(("guest", "anonymous")):
        return True
    normalized_roles = {role.lower() for role in roles}
    return bool(normalized_roles) and normalized_roles <= {"guest"}


def employee_id_from_ref(ref: str) -> str | None:
    path, _fragment = split_ref_fragment(ref)
    normalized = normalize_runtime_path(path)
    parts = [part for part in normalized.split("/") if part]
    if len(parts) == 4 and parts[:2] == ["proc", "staff"]:
        employee_id = parts[3].removesuffix(".json")
        return employee_id if EMPLOYEE_ID_RE.match(employee_id) else None

    if len(parts) != 3 or parts[:2] != ["proc", "employees"]:
        return None

    employee_id = parts[2].removesuffix(".json")
    return employee_id if EMPLOYEE_ID_RE.match(employee_id) else None


def return_id_from_ref(ref: str) -> str | None:
    path, _fragment = split_ref_fragment(ref)
    normalized = normalize_runtime_path(path)
    parts = [part for part in normalized.split("/") if part]
    if len(parts) >= 4 and parts[:2] == ["proc", "return-workflows"]:
        return_id = parts[-1].removesuffix(".json")
        return (
            return_id
            if return_id.startswith(("return-", "return_", "ret-", "ret_"))
            else None
        )

    if len(parts) != 3 or parts[:2] != ["proc", "returns"]:
        return None

    return_id = parts[2].removesuffix(".json")
    return (
        return_id
        if return_id.startswith(("return-", "return_", "ret-", "ret_"))
        else None
    )


def linked_payment_refs_for_returns(vm: RuntimeVM, refs: list[str]) -> list[str]:
    linked_refs: list[str] = []
    unresolved_return_ids: list[str] = []
    for ref in refs:
        return_id = return_id_from_ref(ref)
        if not return_id:
            continue

        path = normalize_runtime_path(split_ref_fragment(ref)[0])
        return_record = read_json_record(vm, path)
        if return_record is None and not path.endswith(".json"):
            return_record = read_json_record(vm, f"{path}.json")
        if return_record is None:
            unresolved_return_ids.append(return_id)
            continue

        payment_id = str(return_record.data.get("payment_id") or "")
        if not payment_id:
            continue
        payment_record = find_record_by_id(
            vm,
            PAYMENT_ROOTS,
            payment_id,
            customer_id=record_customer_id(return_record) or None,
        )
        if payment_record:
            linked_refs.append(payment_record.path)

    if linked_refs:
        return dedupe_refs(linked_refs)

    return_ids = unresolved_return_ids
    if not return_ids:
        return []

    return_values = ", ".join(sql_quote(return_id) for return_id in return_ids)
    rows = sql_rows(
        vm,
        "select distinct p.record_path as payment_record_path "
        "from return_requests r "
        "join payment_transactions p on p.payment_id = r.payment_id "
        f"where r.return_id in ({return_values}) "
        "order by p.record_path;",
    )
    return dedupe_refs(
        [
            row.get("payment_record_path") or ""
            for row in rows
            if (row.get("payment_record_path") or "").startswith("/")
        ]
    )


def store_ref_for_employee(vm: RuntimeVM, employee_id: str) -> str | None:
    employee_record = find_record_by_id(vm, STAFF_ROOTS, employee_id)
    if employee_record is not None:
        store_id = str(employee_record.data.get("store_id") or "")
        store_record = find_record_by_id(vm, STORE_ROOTS, store_id)
        if store_record is not None:
            return store_record.path

    rows = sql_rows(
        vm,
        "select s.record_path as store_record_path "
        "from employee_accounts e "
        "join stores s on s.store_id = e.store_id "
        f"where e.employee_id = {sql_quote(employee_id)} "
        "limit 1;",
    )
    if not rows:
        return None

    path = rows[0].get("store_record_path") or ""
    return path if path.startswith("/") else None


def employee_ref_by_id(vm: RuntimeVM, employee_id: str) -> str | None:
    path_from_sql = sql_record_path(
        vm,
        table="employee_accounts",
        key_column="employee_id",
        value=employee_id,
    )
    if path_from_sql and try_stat(vm, path_from_sql):
        return path_from_sql

    employee_record = find_record_by_id(vm, STAFF_ROOTS, employee_id)
    if employee_record is not None:
        return employee_record.path
    return None


def replace_customer_facing_employee_refs(
    vm: RuntimeVM,
    refs: list[str],
    *,
    user_id: str | None,
    roles: set[str],
) -> list[str]:
    if not is_customer_or_guest_context(user_id, roles):
        return refs

    replaced_refs: list[str] = []
    for ref in refs:
        employee_id = employee_id_from_ref(ref)
        if employee_id is None:
            replaced_refs.append(ref)
            continue

        # Employee profiles include private operational contact data. Customer
        # and guest answers may use them internally, but final grounding should
        # cite the associated store record instead when that relationship exists.
        store_ref = store_ref_for_employee(vm, employee_id)
        if store_ref:
            replaced_refs.append(store_ref)

    return dedupe_refs(replaced_refs)


def is_cross_customer_protected_record_denial(
    cmd: CompletionLike,
    row_refs: Sequence[str],
) -> bool:
    if getattr(cmd, "outcome", "") != "OUTCOME_DENIED_SECURITY":
        return False

    normalized_refs = [normalize_runtime_path(split_ref_fragment(ref)[0]) for ref in row_refs]
    if not any(CUSTOMER_SCOPED_REF_RE.match(ref) for ref in normalized_refs):
        return False

    message = getattr(cmd, "message", "")
    return bool(CROSS_CUSTOMER_DENIAL_RE.search(message))


def is_guest_identity(user_id: str | None, roles: set[str]) -> bool:
    normalized_user = (user_id or "").lower()
    normalized_roles = {role.lower() for role in roles}
    return (
        (not normalized_user and normalized_roles <= {"guest"})
        or normalized_user.startswith(("guest", "anonymous"))
        or normalized_roles <= {"guest"}
    )


def is_negative_catalog_answer(message: str) -> bool:
    normalized = " ".join(message.lower().split()).strip()
    return normalized in {"<no>", "no", "nein", "false"} or (
        normalized.startswith("false(") and normalized.endswith(")")
    )


def is_positive_availability_answer(message: str) -> bool:
    normalized = " ".join(message.lower().split()).strip()
    return normalized in {"<yes>", "yes", "ja", "true"} or (
        normalized.startswith("true(") and normalized.endswith(")")
    )


def should_preserve_negated_availability_refs(
    cmd: CompletionLike,
    *,
    task_text: str,
) -> bool:
    if cmd.task_type != "availability_count":
        return False
    if task_has_primary_negative_product_description(task_text):
        return False
    if getattr(cmd, "outcome", "") == "OUTCOME_NONE_CLARIFICATION":
        return True
    return not is_positive_availability_answer(cmd.message)


def task_has_primary_negative_product_description(task_text: str) -> bool:
    before_parenthetical = task_text.split("(", 1)[0].lower()
    return " not the " in f" {before_parenthetical} "


def is_crosslist_report_task(task_text: str, message: str) -> bool:
    combined = f"{task_text}\n{message}".lower()
    return "/exports/crosslist" in combined and "purchase request" in combined


def filter_crosslist_refs(row_refs: Sequence[str]) -> list[str]:
    return dedupe_refs(
        [
            ref
            for ref in row_refs
            if UPLOAD_REF_RE.match(normalize_runtime_path(split_ref_fragment(ref)[0]))
        ]
    )


def basket_id_from_ref(ref: str) -> str | None:
    path = normalize_runtime_path(split_ref_fragment(ref)[0])
    match = BASKET_REF_RE.match(path)
    if not match:
        return None
    return match.group("basket").replace("_", "-")


def filter_extra_basket_refs_named_in_message(
    row_refs: Sequence[str],
    message: str,
) -> list[str]:
    baskets_in_refs = {
        basket_id
        for ref in row_refs
        if (basket_id := basket_id_from_ref(ref)) is not None
    }
    if len(baskets_in_refs) <= 1:
        return list(row_refs)

    normalized_message = message.replace("_", "-").lower()
    baskets_in_message = {
        basket_id
        for basket_id in baskets_in_refs
        if basket_id.lower() in normalized_message
    }
    if len(baskets_in_message) != 1:
        return list(row_refs)

    return [
        ref
        for ref in row_refs
        if (basket_id := basket_id_from_ref(ref)) is None
        or basket_id in baskets_in_message
    ]


def explicit_sku_inventory_count_task(task_text: str) -> bool:
    normalized = " ".join(task_text.lower().split())
    if not MESSAGE_SKU_RE.search(task_text):
        return False
    return "sku" in normalized and any(
        marker in normalized
        for marker in (
            "same-day units",
            "physically on hand",
            "after reservations",
            "available after reservations",
        )
    )


def plain_integer_message(message: str) -> int | None:
    normalized = message.strip()
    if not normalized or not normalized.isdecimal():
        return None
    return int(normalized)


def positive_availability_count_message(message: str) -> int | None:
    normalized = " ".join(message.lower().split()).strip()
    if not normalized.startswith("true(") or not normalized.endswith(")"):
        return None
    value = normalized.removeprefix("true(").removesuffix(")")
    if not value.isdecimal():
        return None
    return int(value)


def align_count_catalog_refs_to_answer(
    row_refs: Sequence[str],
    *,
    message: str,
    task_text: str,
) -> list[str]:
    count = plain_integer_message(message)
    if count is None or explicit_sku_inventory_count_task(task_text):
        return list(row_refs)

    catalog_refs = [ref for ref in row_refs if is_catalog_ref(ref)]
    if len(catalog_refs) <= count:
        return list(row_refs)

    kept_catalog_refs = set(catalog_refs[:count])
    return [
        ref
        for ref in row_refs
        if not is_catalog_ref(ref) or ref in kept_catalog_refs
    ]


def align_positive_availability_refs_to_answer(
    row_refs: Sequence[str],
    *,
    message: str,
) -> list[str]:
    count = positive_availability_count_message(message)
    if count is None:
        return list(row_refs)

    catalog_refs = [ref for ref in row_refs if is_catalog_ref(ref)]
    if len(catalog_refs) <= count:
        return list(row_refs)

    kept_catalog_refs = set(catalog_refs[:count])
    return [
        ref
        for ref in row_refs
        if not is_catalog_ref(ref) or ref in kept_catalog_refs
    ]


def align_catalog_clarification_refs_to_message(
    vm: RuntimeVM,
    row_refs: Sequence[str],
    *,
    message: str,
) -> list[str]:
    message_catalog_refs = set(
        normalize_runtime_path(split_ref_fragment(ref)[0])
        for ref in sku_refs(vm, MESSAGE_SKU_RE.findall(message))
    )
    if not message_catalog_refs:
        return list(row_refs)

    return [
        ref
        for ref in row_refs
        if not is_catalog_ref(ref)
        or normalize_runtime_path(split_ref_fragment(ref)[0]) in message_catalog_refs
    ]


def submission_refs(
    cmd: CompletionLike,
    vm: RuntimeVM | None = None,
    *,
    task_text: str = "",
) -> list[str]:
    all_refs = [
        *cmd.grounding_doc_refs,
        *cmd.grounding_row_refs,
    ]
    doc_refs = dedupe_refs(
        [
            ref
            for ref in all_refs
            if is_document_ref(ref) and not is_runtime_navigation_doc_ref(ref)
        ]
    )
    row_refs = dedupe_refs([ref for ref in all_refs if not is_document_ref(ref)])

    protected_record_denial = (
        cmd.protected_record_denial
        or is_cross_customer_protected_record_denial(cmd, row_refs)
    )
    user_id: str | None = None
    roles: set[str] = set()
    if (
        not protected_record_denial
        and vm is not None
        and getattr(cmd, "outcome", "") == "OUTCOME_DENIED_SECURITY"
    ):
        user_id, roles = runtime_identity(vm)
        protected_record_denial = has_cross_customer_denial_ref(
            vm,
            row_refs,
            user_id=user_id,
        )
        if (
            not protected_record_denial
            and is_guest_identity(user_id, roles)
            and any(
                CUSTOMER_SCOPED_REF_RE.match(
                    normalize_runtime_path(split_ref_fragment(ref)[0])
                )
                for ref in row_refs
            )
        ):
            protected_record_denial = True

    if protected_record_denial:
        refs = doc_refs
    else:
        if vm is not None and user_id is None and not roles:
            user_id, roles = runtime_identity(vm)
        if is_crosslist_report_task(task_text, cmd.message):
            row_refs = filter_crosslist_refs(row_refs)

        # The final answer is graded on refs separately from the text. When the
        # user names an exact basket/payment/return/customer id, preserve that
        # target evidence even if the model forgets to echo it in grounding refs.
        if vm is not None and task_text and cmd.task_type != "count":
            row_refs = dedupe_refs(
                [
                    *row_refs,
                    *explicit_target_refs_from_task(
                        vm,
                        task_text,
                        user_id=user_id,
                        roles=roles,
                    ),
                ]
            )
        if vm is not None and cmd.task_type in {
            "availability_count",
            "availability_lookup",
            "receipt_price_check",
            "checkout",
        }:
            task_sku_row_refs = task_sku_refs(vm, task_text)
            if cmd.task_type == "availability_count" and catalog_refs_from_refs(row_refs):
                if (
                    not is_positive_availability_answer(cmd.message)
                    or task_has_primary_negative_product_description(task_text)
                ):
                    excluded_refs = set(negated_task_sku_refs(vm, task_text))
                    task_sku_row_refs = [
                        ref
                        for ref in task_sku_row_refs
                        if normalize_runtime_path(split_ref_fragment(ref)[0])
                        not in excluded_refs
                    ]
            row_refs = dedupe_refs(
                [
                    *row_refs,
                    *task_sku_row_refs,
                    *completion_step_sku_refs(vm, cmd),
                    *upload_receipt_sku_refs(vm, row_refs),
                ]
            )
            if should_preserve_negated_availability_refs(cmd, task_text=task_text):
                row_refs = dedupe_refs(
                    [
                        *row_refs,
                        *negated_task_sku_refs(vm, task_text),
                    ]
                )
            if cmd.task_type == "availability_count":
                row_refs = align_positive_availability_refs_to_answer(
                    row_refs,
                    message=cmd.message,
                )
        if (
            vm is not None
            and cmd.task_type in {"availability_count", "availability_lookup", "catalog_lookup"}
            and getattr(cmd, "outcome", "") == "OUTCOME_NONE_CLARIFICATION"
        ):
            row_refs = align_catalog_clarification_refs_to_message(
                vm,
                row_refs,
                message=cmd.message,
            )
            if should_preserve_negated_availability_refs(cmd, task_text=task_text):
                row_refs = dedupe_refs(
                    [
                        *row_refs,
                        *negated_task_sku_refs(vm, task_text),
                    ]
                )
        if (
            vm is not None
            and cmd.task_type == "count"
            and explicit_sku_inventory_count_task(task_text)
        ):
            row_refs = dedupe_refs([*row_refs, *task_sku_refs(vm, task_text)])
        if vm is not None and task_text and cmd.task_type == "count":
            excluded_refs = set(negated_task_sku_refs(vm, task_text))
            if excluded_refs:
                row_refs = [
                    ref
                    for ref in row_refs
                    if normalize_runtime_path(split_ref_fragment(ref)[0])
                    not in excluded_refs
                ]
            row_refs = align_count_catalog_refs_to_answer(
                row_refs,
                message=cmd.message,
                task_text=task_text,
            )
        if cmd.task_type == "catalog_lookup" and is_negative_catalog_answer(cmd.message):
            row_refs = [
                ref
                for ref in row_refs
                if not CATALOG_REF_RE.match(
                    normalize_runtime_path(split_ref_fragment(ref)[0])
                )
            ]
        row_refs = filter_extra_basket_refs_named_in_message(row_refs, cmd.message)
        # Auto-pin every product SKU named in the final message. The grader
        # treats any SKU we surfaced to the user as evidence that must be
        # cited, and quote/table answers routinely list more SKUs than the
        # model remembers to mirror into grounding_row_refs.
        if vm is not None and cmd.task_type != "count":
            row_refs = dedupe_refs(
                [*row_refs, *message_sku_refs(vm, cmd.message)]
            )
        if vm is not None and cmd.task_type == "refund":
            row_refs = dedupe_refs([*row_refs, *linked_payment_refs_for_returns(vm, row_refs)])
        if (
            vm is not None
            and cmd.task_type == "discount"
            and user_id
            and EMPLOYEE_ID_RE.match(user_id)
        ):
            employee_ref = employee_ref_by_id(vm, user_id)
            if employee_ref:
                row_refs = dedupe_refs([*row_refs, employee_ref])
        if vm is not None:
            row_refs = replace_customer_facing_employee_refs(
                vm,
                row_refs,
                user_id=user_id,
                roles=roles,
            )
        refs = dedupe_refs([*doc_refs, *row_refs])

    if vm is None:
        return refs
    return normalize_submission_refs(vm, refs)
