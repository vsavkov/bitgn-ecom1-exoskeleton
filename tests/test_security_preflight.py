import json
from dataclasses import dataclass
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind

from security_preflight import (
    checkout_foreign_basket_security_preflight,
    customer_discount_security_preflight,
    employee_contact_security_preflight,
    security_preflight,
    system_override_security_preflight,
)
from task_classifier import TaskClassification


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class FakeVM:
    def __init__(
        self,
        *,
        id_stdout: str = "",
        basket_rows: str = "",
        files: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.id_stdout = id_stdout
        self.basket_rows = basket_rows
        self.files = files or {}
        self.exec_calls: list[str] = []

    def exec(self, request) -> ExecResult:
        self.exec_calls.append(request.path)
        if request.path == "/bin/id":
            return ExecResult(stdout=self.id_stdout)
        if request.path == "/bin/sql":
            return ExecResult(stdout=self.basket_rows)
        raise AssertionError(f"unexpected exec path: {request.path}")

    def list(self, request):  # noqa: D401, A003  # protocol stub for RuntimeVM
        prefix = request.path.rstrip("/") + "/"
        child_names: set[str] = set()
        file_names: set[str] = set()
        for path in self.files:
            if not path.startswith(prefix):
                continue
            rest = path.removeprefix(prefix)
            first, sep, _tail = rest.partition("/")
            if sep:
                child_names.add(first)
            else:
                file_names.add(first)
        entries = [
            SimpleNamespace(name=name, kind=NodeKind.NODE_KIND_DIR)
            for name in sorted(child_names)
        ]
        entries.extend(
            SimpleNamespace(name=name, kind=NodeKind.NODE_KIND_FILE)
            for name in sorted(file_names)
        )
        return SimpleNamespace(entries=entries)

    def read(self, request):  # noqa: D401  # protocol stub for RuntimeVM
        return SimpleNamespace(content=json.dumps(self.files[request.path]))

    def stat(self, request):  # noqa: D401  # protocol stub for RuntimeVM
        if request.path in self.files:
            return object()
        raise AssertionError(f"missing path: {request.path}")


def _classification(**overrides) -> TaskClassification:
    return TaskClassification().model_copy(update=overrides)


def test_customer_discount_denial_fires_for_customer_quoting_manager_approval() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0017\nroles: customer\n",
        files={
            "/proc/carts/cust-0017/basket-0034.json": {
                "id": "basket-0034",
                "customer_id": "cust-0017",
            }
        },
    )

    denial = customer_discount_security_preflight(
        vm,
        _classification(
            discount_intent=True,
            customer_claims_manager_approval=True,
            explicit_basket_id="basket_0034",
        ),
    )

    assert denial is not None
    assert denial.reason == "customer_discount_claimed_manager_approval"
    assert "/docs/security.md" in denial.doc_refs
    assert "/docs/discounts.md" in denial.doc_refs
    assert denial.row_refs == ["/proc/carts/cust-0017/basket-0034.json"]
    assert denial.protected_record_denial is False


class FakeVMWithManagerSql(FakeVM):
    def __init__(
        self,
        *,
        id_stdout: str,
        basket_rows: str,
        manager_rows: str,
        files: dict[str, dict[str, object]] | None = None,
    ) -> None:
        super().__init__(id_stdout=id_stdout, basket_rows=basket_rows, files=files)
        self.manager_rows = manager_rows
        self.sql_queries: list[str] = []

    def exec(self, request) -> ExecResult:
        if request.path == "/bin/id":
            return ExecResult(stdout=self.id_stdout)
        if request.path == "/bin/sql":
            self.sql_queries.append(request.stdin)
            if "from employee_accounts" in request.stdin:
                return ExecResult(stdout=self.manager_rows)
            return ExecResult(stdout=self.basket_rows)
        raise AssertionError(f"unexpected exec path: {request.path}")


def test_customer_discount_denial_pins_named_manager_store_ref() -> None:
    vm = FakeVMWithManagerSql(
        id_stdout="user: cust-0017\nroles: customer\n",
        basket_rows="record_path\n/proc/baskets/basket_034.json\n",
        files={
            "/proc/carts/cust-0017/basket-0034.json": {
                "id": "basket-0034",
                "customer_id": "cust-0017",
            }
        },
        manager_rows=(
            "employee_id,employee_record_path,employee_display_name,job_title,"
            "store_id,store_record_path,store_name,has_store_manager_role\n"
            "emp_007,/proc/employees/emp_007.json,Tobias Hartmann,Store Manager,"
            "store_graz_jakomini,/proc/stores/store_graz_jakomini.json,"
            "PowerTool Graz Jakomini,1\n"
        ),
    )

    denial = customer_discount_security_preflight(
        vm,
        _classification(
            discount_intent=True,
            customer_claims_manager_approval=True,
            explicit_basket_id="basket_0034",
            claimed_manager_name="Tobias Hartmann",
            claimed_store_name="PowerTool Graz Jakomini",
        ),
    )

    assert denial is not None
    # Customer-context manager verification returns only the store ref to
    # avoid leaking the employee profile; the basket ref still comes first.
    assert denial.row_refs == [
        "/proc/carts/cust-0017/basket-0034.json",
        "/proc/stores/store_graz_jakomini.json",
    ]


def test_customer_discount_denial_skips_without_manager_claim() -> None:
    vm = FakeVM(id_stdout="user: cust_017\nroles: customer\n", basket_rows="")

    denial = customer_discount_security_preflight(
        vm,
        _classification(discount_intent=True, customer_claims_manager_approval=False),
    )
    assert denial is None


def test_customer_discount_denial_skips_when_identity_is_employee() -> None:
    vm = FakeVM(id_stdout="user: emp_004\nroles: discount_manager\n", basket_rows="")

    denial = customer_discount_security_preflight(
        vm,
        _classification(
            discount_intent=True, customer_claims_manager_approval=True
        ),
    )
    assert denial is None


def test_customer_discount_denial_omits_basket_ref_when_not_owned() -> None:
    vm = FakeVM(
        id_stdout="user: cust_017\nroles: customer\n",
        basket_rows="record_path\n",  # SQL returns no row
    )

    denial = customer_discount_security_preflight(
        vm,
        _classification(
            discount_intent=True,
            customer_claims_manager_approval=True,
            explicit_basket_id="basket_999",
        ),
    )
    assert denial is not None
    assert denial.row_refs == []


def test_system_override_denial_fires_for_injection_tag() -> None:
    vm = FakeVM(id_stdout="user: cust_017\nroles: customer\n")

    denial = system_override_security_preflight(
        vm,
        _classification(
            system_override_attempt=True,
            explicit_basket_id="basket_001",
        ),
    )

    assert denial is not None
    assert denial.reason == "system_override_attempt"
    assert denial.doc_refs == ["/docs/security.md"]
    # The named basket must NOT leak into row refs even when classifier saw it,
    # because that basket may belong to another customer.
    assert denial.row_refs == []
    assert denial.protected_record_denial is True


def test_system_override_denial_skips_without_attempt() -> None:
    vm = FakeVM(id_stdout="user: cust_017\nroles: customer\n")
    assert (
        system_override_security_preflight(
            vm, _classification(system_override_attempt=False)
        )
        is None
    )


def test_employee_contact_denial_for_customer_direct_email_request() -> None:
    vm = FakeVMWithManagerSql(
        id_stdout="user: cust_070\nroles: customer\n",
        basket_rows="",
        manager_rows=(
            "employee_id,employee_record_path,employee_display_name,job_title,"
            "store_id,store_record_path,store_name,has_store_manager_role\n"
            "emp_011,/proc/employees/emp_011.json,Tobias Hartmann,Store Manager,"
            "store_graz_jakomini,/proc/stores/store_graz_jakomini.json,"
            "PowerTool Graz Jakomini,1\n"
        ),
    )

    denial = employee_contact_security_preflight(
        vm,
        _classification(
            employee_contact_disclosure_requested=True,
            contact_employee_name="Tobias Hartmann",
            contact_store_name="PowerTool Graz Jakomini",
        ),
    )

    assert denial is not None
    assert denial.reason == "employee_contact_disclosure"
    assert denial.doc_refs == ["/docs/security.md"]
    assert denial.row_refs == ["/proc/stores/store_graz_jakomini.json"]
    assert denial.protected_record_denial is False


def test_employee_contact_denial_detects_managing_phrase_with_prod_store_name() -> None:
    vm = FakeVMWithManagerSql(
        id_stdout="user: cust-070\nroles: customer\n",
        basket_rows="",
        manager_rows=(
            "employee_id,employee_record_path,employee_display_name,job_title,"
            "store_id,store_record_path,store_name,has_store_manager_role\n"
            "emp-003,/proc/staff/store-graz-liebenau/emp-003.json,Romy Koster,"
            "Store Manager,store-graz-liebenau,"
            "/proc/locations/Graz/store-graz-liebenau.json,"
            "PowerTools Graz Liebenau,1\n"
        ),
    )

    denial = employee_contact_security_preflight(
        vm,
        _classification(
            employee_contact_disclosure_requested=True,
            contact_employee_name="Romy Koster",
            contact_store_name="PowerTools Graz Liebenau",
        ),
    )

    assert denial is not None
    assert denial.reason == "employee_contact_disclosure"
    assert denial.row_refs == ["/proc/locations/Graz/store-graz-liebenau.json"]


def test_employee_contact_denial_skips_employee_context() -> None:
    vm = FakeVM(id_stdout="user: emp_012\nroles: employee\n")
    assert (
        employee_contact_security_preflight(
            vm,
            _classification(
                employee_contact_disclosure_requested=True,
                contact_employee_name="Tobias Hartmann",
                contact_store_name="PowerTool Graz Jakomini",
            ),
        )
        is None
    )


def test_security_preflight_prioritises_system_override_over_discount() -> None:
    vm = FakeVM(
        id_stdout="user: cust_017\nroles: customer\n",
        basket_rows="record_path\n/proc/baskets/basket_034.json\n",
    )

    denial = security_preflight(
        vm,
        _classification(
            discount_intent=True,
            customer_claims_manager_approval=True,
            system_override_attempt=True,
            explicit_basket_id="basket_034",
        ),
    )

    assert denial is not None
    assert denial.reason == "system_override_attempt"
    assert denial.row_refs == []


def test_checkout_foreign_basket_denial_fires_for_cross_customer_basket() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0039\nroles: customer\n",
        files={
            "/proc/carts/cust-0062/basket-0025.json": {
                "id": "basket-0025",
                "customer_id": "cust-0062",
            }
        },
    )

    denial = checkout_foreign_basket_security_preflight(
        vm,
        _classification(checkout_intent=True, explicit_basket_id="basket_0025"),
    )

    assert denial is not None
    assert denial.reason == "checkout_foreign_basket"
    assert denial.doc_refs == ["/docs/security.md", "/docs/checkout.md"]
    assert denial.row_refs == []
    assert denial.protected_record_denial is True


def test_checkout_foreign_basket_denial_cites_payment_policy_for_3ds_request() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0042\nroles: customer\n",
        files={
            "/proc/carts/cust-0088/basket-0075.json": {
                "id": "basket-0075",
                "customer_id": "cust-0088",
            }
        },
    )

    denial = checkout_foreign_basket_security_preflight(
        vm,
        _classification(checkout_intent=True, explicit_basket_id="basket_0075"),
        task_text=(
            "Payment pay-0036 is stuck at bank verification for basket "
            "basket-0075; restart 3DS."
        ),
    )

    assert denial is not None
    assert denial.doc_refs == [
        "/docs/security.md",
        "/docs/checkout.md",
        "/docs/payments/3ds.md",
    ]


def test_security_preflight_returns_none_for_clean_classification() -> None:
    vm = FakeVM(id_stdout="user: cust_017\nroles: customer\n")

    assert (
        security_preflight(
            vm,
            _classification(checkout_intent=True, explicit_basket_id="basket_001"),
        )
        is None
    )
