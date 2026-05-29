from dataclasses import dataclass

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
