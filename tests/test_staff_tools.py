from collections.abc import Sequence
import json
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind

from task_classifier import TaskClassification
from staff_tools import staff_role_count_preflight, verify_store_manager_filesystem


class FakeVM:
    def __init__(
        self,
        *,
        entries_by_path: dict[str, Sequence[SimpleNamespace]],
        file_payloads: dict[str, dict],
        id_stdout: str = "user: emp-0001\nroles: employee\n",
    ) -> None:
        self.entries_by_path = entries_by_path
        self.file_payloads = file_payloads
        self.id_stdout = id_stdout

    def list(self, request):
        return SimpleNamespace(entries=self.entries_by_path.get(request.path, []))

    def read(self, request):
        return SimpleNamespace(content=json.dumps(self.file_payloads[request.path]))

    def exec(self, request):
        if request.path == "/bin/id":
            return SimpleNamespace(stdout=self.id_stdout, stderr="", exit_code=0)
        raise AssertionError(f"unexpected exec path: {request.path}")


def _entry(path: str, kind: NodeKind) -> SimpleNamespace:
    return SimpleNamespace(path=path, name=path.rsplit("/", 1)[-1], kind=kind)


def _staff_vm(id_stdout: str = "user: emp-0001\nroles: employee\n") -> FakeVM:
    return FakeVM(
        entries_by_path={
            "/proc/staff": [
                _entry("/proc/staff/store-vienna-hietzing", NodeKind.NODE_KIND_DIR),
                _entry("/proc/staff/store-graz-liebenau", NodeKind.NODE_KIND_DIR),
            ],
            "/proc/staff/store-vienna-hietzing": [
                _entry(
                    "/proc/staff/store-vienna-hietzing/emp-0001.json",
                    NodeKind.NODE_KIND_FILE,
                ),
                _entry(
                    "/proc/staff/store-vienna-hietzing/emp-0002.json",
                    NodeKind.NODE_KIND_FILE,
                ),
            ],
            "/proc/staff/store-graz-liebenau": [
                _entry(
                    "/proc/staff/store-graz-liebenau/emp-0003.json",
                    NodeKind.NODE_KIND_FILE,
                ),
            ],
            "/proc/locations": [
                _entry("/proc/locations/Vienna", NodeKind.NODE_KIND_DIR),
                _entry("/proc/locations/Graz", NodeKind.NODE_KIND_DIR),
            ],
            "/proc/locations/Vienna": [
                _entry(
                    "/proc/locations/Vienna/store-vienna-hietzing.json",
                    NodeKind.NODE_KIND_FILE,
                ),
            ],
            "/proc/locations/Graz": [
                _entry(
                    "/proc/locations/Graz/store-graz-liebenau.json",
                    NodeKind.NODE_KIND_FILE,
                ),
            ],
        },
        file_payloads={
            "/proc/staff/store-vienna-hietzing/emp-0001.json": {
                "id": "emp-0001",
                "display_name": "Romy Koster",
                "store_id": "store-vienna-hietzing",
                "roles": ["store_manager", "customer_service"],
            },
            "/proc/staff/store-vienna-hietzing/emp-0002.json": {
                "id": "emp-0002",
                "display_name": "Milan Berger",
                "store_id": "store-vienna-hietzing",
                "roles": ["customer_service"],
            },
            "/proc/staff/store-graz-liebenau/emp-0003.json": {
                "id": "emp-0003",
                "display_name": "Lea Fuchs",
                "store_id": "store-graz-liebenau",
                "roles": ["store_manager"],
            },
            "/proc/locations/Vienna/store-vienna-hietzing.json": {
                "id": "store-vienna-hietzing",
                "name": "PowerTools Vienna Hietzing",
            },
            "/proc/locations/Graz/store-graz-liebenau.json": {
                "id": "store-graz-liebenau",
                "name": "PowerTools Graz Liebenau",
            },
        },
        id_stdout=id_stdout,
    )


def test_staff_role_count_counts_role_across_all_staff() -> None:
    result = staff_role_count_preflight(
        _staff_vm(),
        TaskClassification(
            staff_role_count_intent=True,
            staff_role_count_role="customer_service",
        ),
    )

    assert result is not None
    assert result.count == 2
    assert result.refs_to_submit == [
        "/proc/staff/store-vienna-hietzing/emp-0001.json",
        "/proc/staff/store-vienna-hietzing/emp-0002.json",
    ]


def test_staff_role_count_filters_to_branch_and_supports_role_before_word() -> None:
    result = staff_role_count_preflight(
        _staff_vm(),
        TaskClassification(
            staff_role_count_intent=True,
            staff_role_count_role="store_manager",
            staff_role_count_store_name="PowerTools Vienna Hietzing",
        ),
    )

    assert result is not None
    assert result.count == 1
    assert result.refs_to_submit == [
        "/proc/locations/Vienna/store-vienna-hietzing.json",
        "/proc/staff/store-vienna-hietzing/emp-0001.json",
    ]


def test_verify_store_manager_filesystem_uses_store_ref_for_customer_context() -> None:
    result = verify_store_manager_filesystem(
        _staff_vm(id_stdout="user: cust-0007\nroles: customer\n"),
        employee_name="Romy Koster",
        store_name="PowerTools Vienna Hietzing",
    )

    assert result is not None
    assert result["verified"] is True
    assert result["refs_to_submit"] == [
        "/proc/locations/Vienna/store-vienna-hietzing.json"
    ]


def test_verify_store_manager_filesystem_uses_employee_ref_for_employee_context() -> None:
    result = verify_store_manager_filesystem(
        _staff_vm(id_stdout="user: emp-0012\nroles: employee\n"),
        employee_name="Romy Koster",
        store_name="PowerTools Vienna Hietzing",
    )

    assert result is not None
    assert result["verified"] is True
    assert result["refs_to_submit"] == [
        "/proc/staff/store-vienna-hietzing/emp-0001.json",
        "/proc/locations/Vienna/store-vienna-hietzing.json",
    ]
