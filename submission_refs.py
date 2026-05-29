import csv
import io
import re
from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest, ListRequest, NodeKind, StatRequest
from connectrpc.errors import ConnectError


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

    def stat(self, request: StatRequest) -> Any: ...


class ExplicitRecordSpec(NamedTuple):
    table: str
    key_column: str
    canonical_prefix: str
    customer_column: str
    pattern: re.Pattern[str]


PRODUCT_SKU_RE = re.compile(r"^[A-Z]{3}-[A-Z0-9]+$")
MESSAGE_SKU_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{3}-[A-Z0-9]{4,}(?![A-Z0-9])")
EMPLOYEE_ID_RE = re.compile(r"^emp_\d+$")
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
    r"^/proc/(?:baskets|customers|payments|returns)/",
    re.IGNORECASE,
)
CATALOG_REF_RE = re.compile(r"^/proc/catalog(?:/|$)", re.IGNORECASE)
PROC_RECORD_TABLES: dict[str, tuple[str, str, re.Pattern[str]]] = {
    "baskets": ("shopping_baskets", "basket_id", re.compile(r"^basket_\d+$")),
    "customers": ("customer_accounts", "customer_id", re.compile(r"^cust_\d+$")),
    "employees": ("employee_accounts", "employee_id", re.compile(r"^emp_\d+$")),
    "payments": ("payment_transactions", "payment_id", re.compile(r"^pay_\d+$")),
    "returns": ("return_requests", "return_id", re.compile(r"^ret_\d+$")),
    "stores": ("stores", "store_id", re.compile(r"^store_[a-z0-9_]+$")),
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


def is_catalog_ref(ref: str) -> bool:
    path, _fragment = split_ref_fragment(ref)
    return bool(CATALOG_REF_RE.match(normalize_runtime_path(path)))


def normalize_runtime_path(path: str) -> str:
    if path == "/":
        return "/"
    return f"/{path.strip('/')}"


def try_stat(vm: RuntimeVM, path: str) -> bool:
    try:
        vm.stat(StatRequest(path=path))
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
        result = vm.list(ListRequest(path=parent))
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
        result = vm.exec(ExecRequest(path="/bin/sql", stdin=query))
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
            roles.update(re.findall(r"[A-Za-z0-9_]+", value))
    return user_id, roles


def runtime_identity(vm: RuntimeVM) -> tuple[str | None, set[str]]:
    try:
        result = vm.exec(ExecRequest(path="/bin/id"))
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
    if user_id.startswith("cust_"):
        return record_customer_id == user_id
    if roles <= {"guest"}:
        return False
    return True


def explicit_record_path_if_allowed(
    vm: RuntimeVM,
    spec: ExplicitRecordSpec,
    record_id: str,
    *,
    user_id: str | None,
    roles: set[str],
) -> str | None:
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
    if user_id and user_id.startswith("cust_"):
        return True
    if user_id and user_id.startswith("guest"):
        return True
    return bool(roles) and roles <= {"guest"}


def employee_id_from_ref(ref: str) -> str | None:
    path, _fragment = split_ref_fragment(ref)
    normalized = normalize_runtime_path(path)
    parts = [part for part in normalized.split("/") if part]
    if len(parts) != 3 or parts[:2] != ["proc", "employees"]:
        return None

    employee_id = parts[2].removesuffix(".json")
    return employee_id if EMPLOYEE_ID_RE.match(employee_id) else None


def return_id_from_ref(ref: str) -> str | None:
    path, _fragment = split_ref_fragment(ref)
    normalized = normalize_runtime_path(path)
    parts = [part for part in normalized.split("/") if part]
    if len(parts) != 3 or parts[:2] != ["proc", "returns"]:
        return None

    return_id = parts[2].removesuffix(".json")
    return return_id if re.match(r"^ret_\d+$", return_id) else None


def linked_payment_refs_for_returns(vm: RuntimeVM, refs: list[str]) -> list[str]:
    return_ids = [return_id for ref in refs if (return_id := return_id_from_ref(ref))]
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
    doc_refs = dedupe_refs([ref for ref in all_refs if is_document_ref(ref)])
    row_refs = dedupe_refs([ref for ref in all_refs if not is_document_ref(ref)])

    protected_record_denial = (
        cmd.protected_record_denial
        or is_cross_customer_protected_record_denial(cmd, row_refs)
    )

    if cmd.task_type == "count" or protected_record_denial:
        refs = doc_refs
    else:
        user_id: str | None = None
        roles: set[str] = set()
        if vm is not None:
            user_id, roles = runtime_identity(vm)

        # The final answer is graded on refs separately from the text. When the
        # user names an exact basket/payment/return/customer id, preserve that
        # target evidence even if the model forgets to echo it in grounding refs.
        if vm is not None and task_text:
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
        # Auto-pin every product SKU named in the final message. The grader
        # treats any SKU we surfaced to the user as evidence that must be
        # cited, and quote/table answers routinely list more SKUs than the
        # model remembers to mirror into grounding_row_refs.
        if vm is not None:
            row_refs = dedupe_refs(
                [*row_refs, *message_sku_refs(vm, cmd.message)]
            )
        if vm is not None and cmd.task_type == "refund":
            row_refs = dedupe_refs([*row_refs, *linked_payment_refs_for_returns(vm, row_refs)])
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
