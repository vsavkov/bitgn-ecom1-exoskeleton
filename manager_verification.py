import csv
import io
import re
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest
from connectrpc.errors import ConnectError
from pydantic import BaseModel, Field

from runtime_calls import runtime_exec
from submission_refs import parse_runtime_identity


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...


class ReqVerifyStoreManager(BaseModel):
    employee_name: str = Field(
        description=(
            "Employee display name from the task, e.g. 'Philipp Lehmann'. "
            "Use the human name, not a role phrase."
        )
    )
    store_name: str = Field(
        description=(
            "Store name from the task, e.g. 'PowerTool Vienna Praterstern'. "
            "Use the full store name when available."
        )
    )


def _sql_rows(vm: RuntimeVM, query: str) -> list[dict[str, str]]:
    try:
        result = runtime_exec(vm, ExecRequest(path="/bin/sql", stdin=query))
    except ConnectError as exc:
        raise RuntimeError(f"manager verification SQL failed: {exc.message}") from exc

    if getattr(result, "exit_code", 0):
        raise RuntimeError(
            "manager verification SQL exited with "
            f"{result.exit_code}: {(result.stderr or '').strip()}"
        )

    stdout = (result.stdout or "").strip()
    if not stdout:
        return []
    try:
        return [dict(row) for row in csv.DictReader(io.StringIO(stdout))]
    except csv.Error as exc:
        raise RuntimeError("manager verification SQL returned invalid CSV") from exc


def _normalized_words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _matches_query(candidate: str, query: str) -> bool:
    candidate_words = _normalized_words(candidate)
    query_words = _normalized_words(query)
    if not candidate_words or not query_words:
        return False
    return query_words <= candidate_words or candidate_words <= query_words


def _is_customer_or_guest(user_id: str | None, roles: set[str]) -> bool:
    if user_id and (user_id.startswith("cust_") or user_id.startswith("guest")):
        return True
    return bool(roles) and roles <= {"guest"}


def _current_identity(vm: RuntimeVM) -> tuple[str | None, set[str]]:
    try:
        result = runtime_exec(vm, ExecRequest(path="/bin/id"))
    except ConnectError:
        return None, set()
    return parse_runtime_identity(result.stdout or "")


def verify_store_manager(vm: RuntimeVM, cmd: ReqVerifyStoreManager) -> dict[str, Any]:
    rows = _sql_rows(
        vm,
        "select "
        "e.employee_id, e.record_path as employee_record_path, "
        "e.employee_display_name, e.job_title, "
        "s.store_id, s.record_path as store_record_path, s.store_name, "
        "case when exists ("
        "select 1 from employee_role_assignments r "
        "where r.employee_id = e.employee_id and r.role_code = 'store_manager'"
        ") then 1 else 0 end as has_store_manager_role "
        "from employee_accounts e "
        "join stores s on s.store_id = e.store_id "
        "order by e.employee_display_name, s.store_name;",
    )

    matched_rows = [
        row
        for row in rows
        if _matches_query(row.get("employee_display_name", ""), cmd.employee_name)
        and _matches_query(row.get("store_name", ""), cmd.store_name)
    ]

    if not matched_rows:
        return {
            "verified": False,
            "reason": "No employee/store match found for the supplied name and store.",
            "employee": None,
            "store": None,
            "refs_to_submit": [],
        }

    row = matched_rows[0]
    verified = row.get("has_store_manager_role") == "1"
    employee_ref = row.get("employee_record_path") or ""
    store_ref = row.get("store_record_path") or ""
    user_id, roles = _current_identity(vm)

    # Employee profile records can contain private operational details. In
    # customer or guest contexts, submit the store record as public evidence and
    # keep the employee record as internal verification detail in the tool result.
    refs_to_submit = [store_ref]
    if not _is_customer_or_guest(user_id, roles) and employee_ref.startswith("/"):
        refs_to_submit = [employee_ref, store_ref]

    return {
        "verified": verified,
        "reason": (
            "Employee has store_manager role at the requested store."
            if verified
            else "Employee is at the requested store but lacks store_manager role."
        ),
        "employee": {
            "employee_id": row.get("employee_id", ""),
            "display_name": row.get("employee_display_name", ""),
            "job_title": row.get("job_title", ""),
            "record_path": employee_ref,
        },
        "store": {
            "store_id": row.get("store_id", ""),
            "store_name": row.get("store_name", ""),
            "record_path": store_ref,
        },
        "refs_to_submit": [ref for ref in refs_to_submit if ref.startswith("/")],
    }
