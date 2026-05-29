import csv
import io
import re
from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest, StatRequest
from connectrpc.errors import ConnectError


class CompletionLike(Protocol):
    @property
    def task_type(self) -> str: ...

    @property
    def protected_record_denial(self) -> bool: ...

    @property
    def grounding_doc_refs(self) -> Sequence[str]: ...

    @property
    def grounding_row_refs(self) -> Sequence[str]: ...


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...

    def stat(self, request: StatRequest) -> Any: ...


class ExplicitRecordSpec(NamedTuple):
    table: str
    key_column: str
    canonical_prefix: str
    customer_column: str
    pattern: re.Pattern[str]


PRODUCT_SKU_RE = re.compile(r"^[A-Z]{3}-[A-Z0-9]+$")
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
            if try_stat(vm, normalized_path):
                normalized_refs.append(f"{normalized_path}{fragment}")
            continue

        canonical = canonical_proc_record_ref(vm, normalized_path)
        if canonical:
            normalized_refs.append(f"{canonical}{fragment}")

    return dedupe_refs(normalized_refs)


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

    if cmd.task_type == "count" or cmd.protected_record_denial:
        refs = doc_refs
    else:
        # The final answer is graded on refs separately from the text. When the
        # user names an exact basket/payment/return/customer id, preserve that
        # target evidence even if the model forgets to echo it in grounding refs.
        if vm is not None and task_text:
            row_refs = dedupe_refs([*row_refs, *explicit_target_refs_from_task(vm, task_text)])
        refs = dedupe_refs([*doc_refs, *row_refs])

    if vm is None:
        return refs
    return normalize_submission_refs(vm, refs)
