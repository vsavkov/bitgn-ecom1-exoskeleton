from dataclasses import dataclass

from security_preflight import (
    customer_discount_security_preflight,
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
    ) -> None:
        self.id_stdout = id_stdout
        self.basket_rows = basket_rows
        self.exec_calls: list[str] = []

    def exec(self, request) -> ExecResult:
        self.exec_calls.append(request.path)
        if request.path == "/bin/id":
            return ExecResult(stdout=self.id_stdout)
        if request.path == "/bin/sql":
            return ExecResult(stdout=self.basket_rows)
        raise AssertionError(f"unexpected exec path: {request.path}")

    def list(self, request):  # noqa: D401, A003  # protocol stub for RuntimeVM
        raise AssertionError("list() not expected in security preflight tests")

    def stat(self, request):  # noqa: D401  # protocol stub for RuntimeVM
        raise AssertionError("stat() not expected in security preflight tests")


def _classification(**overrides) -> TaskClassification:
    return TaskClassification().model_copy(update=overrides)


def test_customer_discount_denial_fires_for_customer_quoting_manager_approval() -> None:
    vm = FakeVM(
        id_stdout="user: cust_017\nroles: customer\n",
        basket_rows=(
            "record_path\n/proc/baskets/basket_034.json\n"
        ),
    )

    denial = customer_discount_security_preflight(
        vm,
        _classification(
            discount_intent=True,
            customer_claims_manager_approval=True,
            explicit_basket_id="basket_034",
        ),
    )

    assert denial is not None
    assert denial.reason == "customer_discount_claimed_manager_approval"
    assert "/docs/security.md" in denial.doc_refs
    assert "/docs/discounts.md" in denial.doc_refs
    assert denial.row_refs == ["/proc/baskets/basket_034.json"]
    assert denial.protected_record_denial is False


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


def test_security_preflight_returns_none_for_clean_classification() -> None:
    vm = FakeVM(id_stdout="user: cust_017\nroles: customer\n")

    assert (
        security_preflight(
            vm,
            _classification(checkout_intent=True, explicit_basket_id="basket_001"),
        )
        is None
    )
