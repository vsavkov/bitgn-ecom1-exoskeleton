import json
from dataclasses import dataclass
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ListRequest, NodeKind, ReadRequest
from connectrpc.errors import ConnectError

from runtime_calls import runtime_list, runtime_read
from submission_refs import parse_runtime_identity
from task_classifier import TaskClassification


class RuntimeVM(Protocol):
    def list(self, request: ListRequest) -> Any: ...

    def read(self, request: ReadRequest) -> Any: ...


@dataclass(frozen=True)
class StaffRecord:
    path: str
    employee_id: str
    display_name: str
    store_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class StoreRecord:
    path: str
    store_id: str
    name: str


@dataclass(frozen=True)
class StaffRoleCountResult:
    count: int
    refs_to_submit: list[str]
    branch_ref: str
    role: str
    completed_steps_laconic: list[str]


def staff_role_count_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> StaffRoleCountResult | None:
    if not classification.staff_role_count_intent:
        return None

    role = _normalize_role(classification.staff_role_count_role)
    if not role:
        return None

    try:
        staff = _load_staff_records(vm)
    except ConnectError:
        return None

    branch_ref = ""
    branch_query = classification.staff_role_count_store_name.strip()
    if branch_query:
        try:
            store = _find_store(vm, branch_query)
        except ConnectError:
            store = None
        if store is None:
            return None
        branch_ref = store.path
        staff = [record for record in staff if record.store_id == store.store_id]

    matching = [record for record in staff if role in record.roles]
    refs = [record.path for record in matching]
    if branch_ref:
        refs = [branch_ref, *refs]

    scope = f"branch {branch_query}" if branch_query else "all staff records"
    return StaffRoleCountResult(
        count=len(matching),
        refs_to_submit=refs,
        branch_ref=branch_ref,
        role=role,
        completed_steps_laconic=[
            f"Loaded {scope} from /proc/staff.",
            f"Counted employee records whose roles include {role}.",
            "Included every counted employee record in grounding refs.",
        ],
    )


def verify_store_manager_filesystem(
    vm: RuntimeVM,
    *,
    employee_name: str,
    store_name: str,
) -> dict[str, Any] | None:
    try:
        store = _find_store(vm, store_name)
        staff = _load_staff_records(vm)
    except ConnectError:
        return None

    if store is None:
        return None

    matched = [
        record
        for record in staff
        if record.store_id == store.store_id
        and _matches_query(record.display_name, employee_name)
    ]
    if not matched:
        return {
            "verified": False,
            "reason": "No employee/store match found for the supplied name and store.",
            "employee": None,
            "store": {"record_path": store.path, "store_id": store.store_id, "store_name": store.name},
            "refs_to_submit": [],
        }

    record = matched[0]
    verified = "store_manager" in record.roles
    user_id, roles = runtime_identity_from_filesystem_unavailable(vm)
    refs_to_submit = [store.path]
    if not _is_customer_or_guest(user_id, roles):
        refs_to_submit = [record.path, store.path]

    return {
        "verified": verified,
        "reason": (
            "Employee has store_manager role at the requested store."
            if verified
            else "Employee is at the requested store but lacks store_manager role."
        ),
        "employee": {
            "employee_id": record.employee_id,
            "record_path": record.path,
            "employee_display_name": record.display_name,
        },
        "store": {
            "store_id": store.store_id,
            "record_path": store.path,
            "store_name": store.name,
        },
        "refs_to_submit": refs_to_submit,
    }


def runtime_identity_from_filesystem_unavailable(vm: Any) -> tuple[str | None, set[str]]:
    try:
        from bitgn.vm.ecom.ecom_pb2 import ExecRequest
        from runtime_calls import runtime_exec

        result = runtime_exec(vm, ExecRequest(path="/bin/id"))
    except Exception:
        return None, set()
    return parse_runtime_identity(getattr(result, "stdout", "") or "")


def _load_staff_records(vm: RuntimeVM) -> list[StaffRecord]:
    records: list[StaffRecord] = []
    for path in _iter_files(vm, "/proc/staff"):
        try:
            payload = _read_json(vm, path)
        except (ConnectError, json.JSONDecodeError):
            continue
        roles = tuple(str(role) for role in payload.get("roles", []) if isinstance(role, str))
        records.append(
            StaffRecord(
                path=path,
                employee_id=str(payload.get("id") or ""),
                display_name=str(payload.get("display_name") or ""),
                store_id=str(payload.get("store_id") or ""),
                roles=roles,
            )
        )
    return records


def _find_store(vm: RuntimeVM, query: str) -> StoreRecord | None:
    stores: list[StoreRecord] = []
    for path in _iter_files(vm, "/proc/locations"):
        try:
            payload = _read_json(vm, path)
        except (ConnectError, json.JSONDecodeError):
            continue
        stores.append(
            StoreRecord(
                path=path,
                store_id=str(payload.get("id") or ""),
                name=str(payload.get("name") or ""),
            )
        )

    for store in stores:
        if _matches_query(store.name, query):
            return store
    return None


def _iter_files(vm: RuntimeVM, root: str) -> list[str]:
    listing = runtime_list(vm, ListRequest(path=root))
    files: list[str] = []
    for entry in getattr(listing, "entries", []) or []:
        path = getattr(entry, "path", "") or f"{root.rstrip('/')}/{entry.name}"
        kind = getattr(entry, "kind", NodeKind.NODE_KIND_UNSPECIFIED)
        if kind == NodeKind.NODE_KIND_DIR:
            files.extend(_iter_files(vm, path))
        elif kind in {NodeKind.NODE_KIND_FILE, NodeKind.NODE_KIND_UNSPECIFIED}:
            files.append(path)
    return files


def _read_json(vm: RuntimeVM, path: str) -> dict[str, Any]:
    result = runtime_read(vm, ReadRequest(path=path, number=False, start_line=0, end_line=0))
    content = getattr(result, "content", "") or ""
    payload = json.loads(content)
    return payload if isinstance(payload, dict) else {}


def _normalize_role(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _normalized_words(value: str) -> set[str]:
    normalized = "".join(
        char.lower() if char.isalnum() else " "
        for char in value.replace("PowerTools", "").replace("PowerTool", "")
    )
    return set(normalized.split())


def _matches_query(candidate: str, query: str) -> bool:
    candidate_words = _normalized_words(candidate)
    query_words = _normalized_words(query)
    if not candidate_words or not query_words:
        return False
    return query_words <= candidate_words or candidate_words <= query_words


def _is_customer_or_guest(user_id: str | None, roles: set[str]) -> bool:
    if user_id and (user_id.startswith("cust") or user_id.startswith("guest")):
        return True
    return bool(roles) and {role.lower() for role in roles} <= {"guest", "customer"}
