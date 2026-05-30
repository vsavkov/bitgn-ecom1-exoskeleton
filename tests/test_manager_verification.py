from dataclasses import dataclass
import json
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind

import manager_verification
from manager_verification import (
    ReqVerifyStoreManager,
    _matches_query,
    _normalized_words,
    verify_store_manager,
)


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class FakeVM:
    def __init__(self, *, id_stdout: str, employee_rows: str) -> None:
        self.id_stdout = id_stdout
        self.employee_rows = employee_rows

    def exec(self, request) -> ExecResult:
        if request.path == "/bin/id":
            return ExecResult(stdout=self.id_stdout)
        if request.path == "/bin/sql":
            return ExecResult(stdout=self.employee_rows)
        raise AssertionError(f"unexpected exec path: {request.path}")


class FilesystemFakeVM(FakeVM):
    def __init__(self) -> None:
        super().__init__(id_stdout="user: cust-0007\nroles: customer\n", employee_rows="")
        self.entries_by_path = {
            "/proc/staff": [
                SimpleNamespace(
                    path="/proc/staff/store-graz-liebenau",
                    name="store-graz-liebenau",
                    kind=NodeKind.NODE_KIND_DIR,
                ),
            ],
            "/proc/staff/store-graz-liebenau": [
                SimpleNamespace(
                    path="/proc/staff/store-graz-liebenau/emp-0003.json",
                    name="emp-0003.json",
                    kind=NodeKind.NODE_KIND_FILE,
                ),
            ],
            "/proc/locations": [
                SimpleNamespace(
                    path="/proc/locations/Graz",
                    name="Graz",
                    kind=NodeKind.NODE_KIND_DIR,
                ),
            ],
            "/proc/locations/Graz": [
                SimpleNamespace(
                    path="/proc/locations/Graz/store-graz-liebenau.json",
                    name="store-graz-liebenau.json",
                    kind=NodeKind.NODE_KIND_FILE,
                ),
            ],
        }
        self.file_payloads = {
            "/proc/staff/store-graz-liebenau/emp-0003.json": {
                "id": "emp-0003",
                "display_name": "Romy Koster",
                "store_id": "store-graz-liebenau",
                "roles": ["store_manager"],
            },
            "/proc/locations/Graz/store-graz-liebenau.json": {
                "id": "store-graz-liebenau",
                "name": "PowerTools Graz Liebenau",
            },
        }

    def list(self, request):
        return SimpleNamespace(entries=self.entries_by_path.get(request.path, []))

    def read(self, request):
        return SimpleNamespace(content=json.dumps(self.file_payloads[request.path]))


def csv_rows(*rows: str) -> str:
    return "\n".join(rows) + "\n"


def manager_rows(*rows: str) -> str:
    return csv_rows(
        "employee_id,employee_record_path,employee_display_name,job_title,"
        "store_id,store_record_path,store_name,has_store_manager_role",
        *rows,
    )


def test_query_word_matching_requires_all_query_words() -> None:
    assert _normalized_words("PowerTool Vienna-Praterstern") == {
        "powertool",
        "vienna",
        "praterstern",
    }
    assert _matches_query("PowerTool Vienna Praterstern", "Vienna Praterstern")
    assert _matches_query("Philipp Lehmann", "verify Philipp Lehmann is manager")
    assert not _matches_query("PowerTool Vienna Praterstern", "Vienna Meidling")
    assert not _matches_query("PowerTool Vienna Praterstern", "")


def test_verify_store_manager_returns_customer_safe_store_ref() -> None:
    vm = FakeVM(
        id_stdout="user: cust_043\nroles: customer\n",
        employee_rows=manager_rows(
            "emp_001,/proc/employees/emp_001.json,Philipp Lehmann,"
            "General Store Manager,store_vienna_praterstern,"
            "/proc/stores/store_vienna_praterstern.json,"
            "PowerTool Vienna Praterstern,1",
        ),
    )

    result = verify_store_manager(
        vm,
        ReqVerifyStoreManager(
            employee_name="Philipp Lehmann",
            store_name="PowerTool Vienna Praterstern",
        ),
    )

    assert result["verified"] is True
    assert result["refs_to_submit"] == ["/proc/stores/store_vienna_praterstern.json"]
    assert result["employee"]["record_path"] == "/proc/employees/emp_001.json"


def test_verify_store_manager_returns_employee_and_store_refs_for_employee_context() -> None:
    vm = FakeVM(
        id_stdout="user: emp_009\nroles: employee,customer_service\n",
        employee_rows=manager_rows(
            "emp_001,/proc/employees/emp_001.json,Philipp Lehmann,"
            "General Store Manager,store_vienna_praterstern,"
            "/proc/stores/store_vienna_praterstern.json,"
            "PowerTool Vienna Praterstern,1",
        ),
    )

    result = verify_store_manager(
        vm,
        ReqVerifyStoreManager(
            employee_name="Philipp Lehmann",
            store_name="PowerTool Vienna Praterstern",
        ),
    )

    assert result["verified"] is True
    assert result["refs_to_submit"] == [
        "/proc/employees/emp_001.json",
        "/proc/stores/store_vienna_praterstern.json",
    ]


def test_verify_store_manager_reports_non_manager_and_missing_match() -> None:
    vm = FakeVM(
        id_stdout="user: emp_009\nroles: employee,customer_service\n",
        employee_rows=manager_rows(
            "emp_002,/proc/employees/emp_002.json,Julia Wolf,"
            "Inventory Specialist,store_vienna_praterstern,"
            "/proc/stores/store_vienna_praterstern.json,"
            "PowerTool Vienna Praterstern,0",
        ),
    )

    result = verify_store_manager(
        vm,
        ReqVerifyStoreManager(
            employee_name="Julia Wolf",
            store_name="PowerTool Vienna Praterstern",
        ),
    )

    assert result["verified"] is False
    assert result["reason"] == "Employee is at the requested store but lacks store_manager role."
    assert result["refs_to_submit"] == [
        "/proc/employees/emp_002.json",
        "/proc/stores/store_vienna_praterstern.json",
    ]

    missing = verify_store_manager(
        vm,
        ReqVerifyStoreManager(
            employee_name="Philipp Lehmann",
            store_name="PowerTool Vienna Praterstern",
        ),
    )

    assert missing["verified"] is False
    assert missing["refs_to_submit"] == []


def test_verify_store_manager_falls_back_to_filesystem_when_sql_fails(monkeypatch) -> None:
    def fail_sql(*_args, **_kwargs):
        raise RuntimeError("manager verification SQL failed: cluster down")

    monkeypatch.setattr(manager_verification, "_sql_rows", fail_sql)

    result = verify_store_manager(
        FilesystemFakeVM(),
        ReqVerifyStoreManager(
            employee_name="Romy Koster",
            store_name="PowerTools Graz Liebenau",
        ),
    )

    assert result["verified"] is True
    assert result["refs_to_submit"] == ["/proc/locations/Graz/store-graz-liebenau.json"]
