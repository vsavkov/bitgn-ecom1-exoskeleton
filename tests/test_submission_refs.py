from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from submission_refs import (
    EXPLICIT_RECORD_SPECS,
    availability_count_refs_from_catalog_result,
    can_auto_cite_customer_scoped_record,
    candidate_record_ids,
    canonical_case_file_ref,
    canonical_proc_record_ref,
    dedupe_refs,
    employee_id_from_ref,
    explicit_target_refs_from_task,
    is_catalog_ref,
    is_cross_customer_protected_record_denial,
    is_document_ref,
    linked_payment_refs_for_returns,
    manager_store_refs_from_task,
    normalize_runtime_path,
    normalize_submission_refs,
    parse_runtime_identity,
    replace_customer_facing_employee_refs,
    split_ref_fragment,
    sql_quote,
    sql_record_path,
    sql_rows,
    sql_readme_refs_from_task,
    submission_refs,
    support_note_refs_from_catalog_result,
)


@dataclass
class CompletionStub:
    task_type: str = "other"
    protected_record_denial: bool = False
    message: str = ""
    grounding_doc_refs: list[str] = field(default_factory=list)
    grounding_row_refs: list[str] = field(default_factory=list)
    outcome: str = "OUTCOME_OK"


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class FakeVM:
    def __init__(
        self,
        *,
        id_stdout: str = "user: cust_060\nroles: customer\n",
        sql_outputs: dict[str, ExecResult] | None = None,
        list_outputs: dict[str, Sequence[str]] | None = None,
        existing_paths: set[str] | None = None,
    ) -> None:
        self.id_stdout = id_stdout
        self.sql_outputs = sql_outputs or {}
        self.list_outputs = list_outputs or {}
        self.existing_paths = existing_paths or set()

    def exec(self, request) -> ExecResult:
        if request.path == "/bin/id":
            return ExecResult(stdout=self.id_stdout)
        if request.path == "/bin/sql":
            query = request.stdin
            for marker, result in self.sql_outputs.items():
                if marker in query:
                    return result
            return ExecResult(stdout="")
        raise AssertionError(f"unexpected exec path: {request.path}")

    def stat(self, request) -> object:
        if request.path in self.existing_paths:
            return object()
        raise ConnectError(Code.NOT_FOUND, f"{request.path} not found")

    def list(self, request) -> object:
        names = self.list_outputs.get(request.path)
        if names is None:
            raise ConnectError(Code.NOT_FOUND, f"{request.path} not found")
        return SimpleNamespace(
            entries=[
                SimpleNamespace(name=name, kind=NodeKind.NODE_KIND_FILE)
                for name in names
            ]
        )


def csv_rows(*rows: str) -> ExecResult:
    return ExecResult(stdout="\n".join(rows) + "\n")


def test_dedupe_refs_strips_blanks_and_keeps_order() -> None:
    assert dedupe_refs([" /docs/a.md ", "", "/docs/a.md", "/proc/x.json"]) == [
        "/docs/a.md",
        "/proc/x.json",
    ]


def test_document_and_path_helpers() -> None:
    assert is_document_ref("/docs/security.md")
    assert not is_document_ref("/proc/baskets/basket_001.json")
    assert is_catalog_ref("/proc/catalog/FST-123.json")
    assert is_catalog_ref("proc/catalog/Brand/FST-123.json#row=1")
    assert not is_catalog_ref("/proc/stores/store_vienna_praterstern.json")
    assert normalize_runtime_path("/") == "/"
    assert normalize_runtime_path("proc/baskets/basket_001") == "/proc/baskets/basket_001"
    assert split_ref_fragment("/archive/payments.csv#row=7") == (
        "/archive/payments.csv",
        "#row=7",
    )


def test_sql_quote_and_rows() -> None:
    vm = FakeVM(
        sql_outputs={
            "from shopping_baskets": csv_rows(
                "basket_id,record_path",
                "basket_001,/proc/baskets/basket_001.json",
            )
        }
    )

    assert sql_quote("O'Reilly") == "'O''Reilly'"
    assert sql_rows(vm, "select * from shopping_baskets;") == [
        {
            "basket_id": "basket_001",
            "record_path": "/proc/baskets/basket_001.json",
        }
    ]


def test_sql_record_path_returns_absolute_path_only() -> None:
    good_vm = FakeVM(
        sql_outputs={
            "basket_id = 'basket_001'": csv_rows(
                "record_path", "/proc/baskets/basket_001.json"
            )
        }
    )
    bad_vm = FakeVM(
        sql_outputs={"basket_id = 'basket_001'": csv_rows("record_path", "relative.json")}
    )

    assert (
        sql_record_path(
            good_vm,
            table="shopping_baskets",
            key_column="basket_id",
            value="basket_001",
        )
        == "/proc/baskets/basket_001.json"
    )
    assert (
        sql_record_path(
            bad_vm,
            table="shopping_baskets",
            key_column="basket_id",
            value="basket_001",
        )
        is None
    )


def test_canonical_proc_record_ref_repairs_extensionless_record_path() -> None:
    vm = FakeVM(existing_paths={"/proc/baskets/basket_001.json"})

    assert (
        canonical_proc_record_ref(vm, "/proc/baskets/basket_001")
        == "/proc/baskets/basket_001.json"
    )


def test_canonical_proc_record_ref_resolves_sku_shortcuts_through_sql() -> None:
    vm = FakeVM(
        sql_outputs={
            "product_sku = 'MAC-123ABC'": csv_rows(
                "record_path",
                "/proc/catalog/Makita/MAC-123ABC.json",
            )
        },
        existing_paths={"/proc/catalog/Makita/MAC-123ABC.json"},
    )

    assert (
        canonical_proc_record_ref(vm, "/proc/catalog/MAC-123ABC.json")
        == "/proc/catalog/Makita/MAC-123ABC.json"
    )


def test_normalize_submission_refs_preserves_docs_archive_rows_and_canonical_records() -> None:
    vm = FakeVM(existing_paths={"/proc/baskets/basket_001.json"})

    assert normalize_submission_refs(
        vm,
        [
            "docs/security.md",
            "/archive/payments.csv#row=2",
            "/proc/baskets/basket_001",
            "/missing",
            "/proc/baskets/basket_001",
        ],
    ) == [
        "/docs/security.md",
        "/archive/payments.csv#row=2",
        "/proc/baskets/basket_001.json",
    ]


def test_canonical_case_file_ref_repairs_upload_filename_case() -> None:
    vm = FakeVM(
        list_outputs={"/uploads": ["receipt_ocr_V71YxpVz.txt"]},
        existing_paths={"/uploads/receipt_ocr_V71YxpVz.txt"},
    )

    assert (
        canonical_case_file_ref(vm, "/uploads/receipt_OCR_V71YxpVz.txt")
        == "/uploads/receipt_ocr_V71YxpVz.txt"
    )
    assert normalize_submission_refs(
        vm,
        ["/uploads/receipt_OCR_V71YxpVz.txt"],
    ) == ["/uploads/receipt_ocr_V71YxpVz.txt"]


def test_availability_count_refs_from_catalog_result_uses_helper_canonical_refs() -> None:
    assert availability_count_refs_from_catalog_result(
        {
            "store_ref": "/proc/stores/store_vienna_praterstern.json",
            "refs_to_submit_for_availability_count": [
                "/proc/catalog/Brand/FST-1.json",
                "/proc/catalog/Brand/FST-1.json",
                None,
            ],
        }
    ) == [
        "/proc/stores/store_vienna_praterstern.json",
        "/proc/catalog/Brand/FST-1.json",
    ]
    assert availability_count_refs_from_catalog_result("not-json") == []


def test_catalog_refs_from_helper_can_be_empty_when_only_store_ref_returned() -> None:
    assert availability_count_refs_from_catalog_result(
        {"store_ref": "/proc/stores/store_graz_lend.json"}
    ) == ["/proc/stores/store_graz_lend.json"]


def test_support_note_refs_from_catalog_result_reads_checked_base_refs() -> None:
    assert support_note_refs_from_catalog_result(
        {
            "items": [
                {
                    "support_note_extra_claim": {
                        "refs_to_submit": [
                            "/proc/catalog/STO-2R84BSHQ.json",
                            "/proc/catalog/STO-2R84BSHQ.json",
                        ]
                    }
                },
                {"support_note_extra_claim": None},
            ]
        }
    ) == ["/proc/catalog/STO-2R84BSHQ.json"]
    assert support_note_refs_from_catalog_result({"items": []}) == []


def test_candidate_record_ids_handles_padding() -> None:
    assert candidate_record_ids("basket", "57") == ["basket_57", "basket_057"]
    assert candidate_record_ids("basket", "057") == ["basket_057"]


def test_parse_runtime_identity() -> None:
    assert parse_runtime_identity("user: cust_060\nroles: customer discount_manager\n") == (
        "cust_060",
        {"customer", "discount_manager"},
    )


def test_can_auto_cite_customer_scoped_record() -> None:
    assert can_auto_cite_customer_scoped_record(
        user_id="cust_060", roles={"customer"}, record_customer_id="cust_060"
    )
    assert not can_auto_cite_customer_scoped_record(
        user_id="cust_060", roles={"customer"}, record_customer_id="cust_061"
    )
    assert not can_auto_cite_customer_scoped_record(
        user_id=None, roles=set(), record_customer_id="cust_060"
    )
    assert not can_auto_cite_customer_scoped_record(
        user_id="guest_1", roles={"guest"}, record_customer_id="cust_060"
    )
    assert can_auto_cite_customer_scoped_record(
        user_id="emp_001", roles={"discount_manager"}, record_customer_id="cust_060"
    )


def test_explicit_target_refs_from_task_accepts_aliases_and_ownership_gate() -> None:
    vm = FakeVM(
        id_stdout="user: cust_060\nroles: customer\n",
        sql_outputs={
            "basket_id = 'basket_057'": csv_rows(
                "record_path,customer_id",
                "/proc/baskets/basket_057.json,cust_060",
            ),
            "payment_id = 'pay_007'": csv_rows(
                "record_path,customer_id",
                "/proc/payments/pay_007.json,cust_999",
            ),
        },
    )

    assert explicit_target_refs_from_task(
        vm,
        "Please apply discount on bask_57 and inspect payment_7.",
    ) == ["/proc/baskets/basket_057.json"]


def test_explicit_target_patterns_do_not_match_embedded_words() -> None:
    spec = EXPLICIT_RECORD_SPECS[0]

    assert spec.pattern.findall("xBasket_057 basket_058 basket-059") == ["058", "059"]


def test_employee_id_from_ref_accepts_canonical_and_extensionless_paths() -> None:
    assert employee_id_from_ref("/proc/employees/emp_001.json") == "emp_001"
    assert employee_id_from_ref("proc/employees/emp_002") == "emp_002"
    assert employee_id_from_ref("/proc/baskets/basket_001.json") is None


def test_replace_customer_facing_employee_refs_uses_store_record() -> None:
    vm = FakeVM(
        sql_outputs={
            "e.employee_id = 'emp_001'": csv_rows(
                "store_record_path",
                "/proc/stores/store_vienna_praterstern.json",
            )
        }
    )

    assert replace_customer_facing_employee_refs(
        vm,
        [
            "/proc/employees/emp_001.json",
            "/proc/baskets/basket_004.json",
        ],
        user_id="cust_043",
        roles={"customer"},
    ) == [
        "/proc/stores/store_vienna_praterstern.json",
        "/proc/baskets/basket_004.json",
    ]


def test_replace_customer_facing_employee_refs_keeps_employee_identity_refs() -> None:
    vm = FakeVM()

    assert replace_customer_facing_employee_refs(
        vm,
        ["/proc/employees/emp_034.json"],
        user_id="guest",
        roles={"guest"},
    ) == []
    assert replace_customer_facing_employee_refs(
        vm,
        ["/proc/employees/emp_034.json"],
        user_id="emp_034",
        roles={"employee", "discount_manager"},
    ) == ["/proc/employees/emp_034.json"]


def test_linked_payment_refs_for_returns_adds_refund_evidence() -> None:
    vm = FakeVM(
        sql_outputs={
            "from return_requests r": csv_rows(
                "payment_record_path",
                "/proc/payments/pay_023.json",
            )
        }
    )

    assert linked_payment_refs_for_returns(
        vm,
        ["/proc/returns/ret_012.json"],
    ) == ["/proc/payments/pay_023.json"]


def test_manager_store_refs_from_task_requires_manager_verification_intent() -> None:
    vm = FakeVM(
        sql_outputs={
            "from stores order by length(store_name) desc": csv_rows(
                "store_name,record_path",
                "PowerTool Vienna Praterstern,/proc/stores/store_vienna_praterstern.json",
                "PowerTool Innsbruck Wilten,/proc/stores/store_innsbruck_wilten.json",
            )
        }
    )

    assert manager_store_refs_from_task(
        vm,
        "Please verify Philipp Lehmann is manager at PowerTool Vienna Praterstern.",
    ) == ["/proc/stores/store_vienna_praterstern.json"]
    assert manager_store_refs_from_task(
        vm,
        "Please check if Greta Engel really manages PowerTool Innsbruck Wilten.",
    ) == ["/proc/stores/store_innsbruck_wilten.json"]
    assert manager_store_refs_from_task(
        vm,
        "For basket basket_053 at PowerTool Innsbruck Wilten, manager approved it.",
    ) == []


def test_sql_readme_refs_from_task_detects_stale_json_sql_instruction() -> None:
    vm = FakeVM(
        list_outputs={
            "/bin": [
                "README.md",
                "sql",
                "sql-readme-2024-07-17.md",
                "sql-readme-2024-08-01.md",
            ]
        }
    )

    assert sql_readme_refs_from_task(
        vm,
        "PS: availability in JSON is stale, trust SQL",
    ) == [
        "/bin/sql-readme-2024-07-17.md",
        "/bin/sql-readme-2024-08-01.md",
    ]
    assert sql_readme_refs_from_task(vm, "Count catalogue products.") == []


def test_submission_refs_drops_rows_for_count_or_protected_denial() -> None:
    vm = FakeVM(existing_paths={"/proc/baskets/basket_001.json"})

    assert submission_refs(
        CompletionStub(
            task_type="count",
            grounding_doc_refs=["/docs/catalogue.md"],
            grounding_row_refs=["/proc/baskets/basket_001.json"],
        ),
        vm,
        task_text="basket_001",
    ) == ["/docs/catalogue.md"]
    assert submission_refs(
        CompletionStub(
            protected_record_denial=True,
            outcome="OUTCOME_DENIED_SECURITY",
            grounding_doc_refs=["/docs/security.md"],
            grounding_row_refs=["/proc/baskets/basket_001.json"],
        ),
        vm,
        task_text="basket_001",
    ) == ["/docs/security.md"]


def test_submission_refs_auto_adds_sql_readme_for_sql_trusted_count() -> None:
    vm = FakeVM(
        list_outputs={"/bin": ["sql", "sql-readme-2024-07-17.md"]},
        existing_paths={"/proc/baskets/basket_001.json"},
    )

    assert submission_refs(
        CompletionStub(
            task_type="count",
            grounding_doc_refs=[
                "/docs/current-updates/catalogue-counting-2024-07-17.md"
            ],
            grounding_row_refs=["/proc/baskets/basket_001.json"],
        ),
        vm,
        task_text="For catalogue count report, trust SQL because JSON is stale.",
    ) == [
        "/docs/current-updates/catalogue-counting-2024-07-17.md",
        "/bin/sql-readme-2024-07-17.md",
    ]


def test_submission_refs_drops_customer_rows_for_cross_customer_denial() -> None:
    vm = FakeVM(existing_paths={"/proc/baskets/basket_001.json"})
    cmd = CompletionStub(
        task_type="checkout",
        message="I cannot use this basket because it belongs to another customer.",
        grounding_doc_refs=["/docs/security.md"],
        grounding_row_refs=["/proc/baskets/basket_001.json"],
        outcome="OUTCOME_DENIED_SECURITY",
    )

    assert is_cross_customer_protected_record_denial(
        cmd,
        cmd.grounding_row_refs,
    )
    assert submission_refs(cmd, vm, task_text="basket_001") == ["/docs/security.md"]


def test_submission_refs_keeps_rows_for_non_protected_policy_denial() -> None:
    vm = FakeVM(existing_paths={"/proc/baskets/basket_001.json"})
    cmd = CompletionStub(
        task_type="discount",
        message="Discount denied because the required manager approval is invalid.",
        grounding_doc_refs=["/docs/security.md", "/docs/discounts.md"],
        grounding_row_refs=["/proc/baskets/basket_001.json"],
        outcome="OUTCOME_DENIED_SECURITY",
    )

    assert not is_cross_customer_protected_record_denial(
        cmd,
        cmd.grounding_row_refs,
    )
    assert submission_refs(cmd, vm) == [
        "/docs/security.md",
        "/docs/discounts.md",
        "/proc/baskets/basket_001.json",
    ]


def test_submission_refs_auto_adds_safe_target_record_from_task_text() -> None:
    vm = FakeVM(
        id_stdout="user: cust_060\nroles: customer\n",
        sql_outputs={
            "basket_id = 'basket_057'": csv_rows(
                "record_path,customer_id",
                "/proc/baskets/basket_057.json,cust_060",
            )
        },
        existing_paths={"/proc/baskets/basket_057.json"},
    )

    assert submission_refs(
        CompletionStub(
            task_type="discount",
            grounding_doc_refs=["/docs/security.md"],
            grounding_row_refs=[],
        ),
        vm,
        task_text="Can you discount basket_057?",
    ) == ["/docs/security.md", "/proc/baskets/basket_057.json"]


def test_submission_refs_replaces_customer_facing_employee_ref_and_adds_store() -> None:
    vm = FakeVM(
        id_stdout="user: cust_043\nroles: customer\n",
        sql_outputs={
            "basket_id = 'basket_004'": csv_rows(
                "record_path,customer_id",
                "/proc/baskets/basket_004.json,cust_043",
            ),
            "e.employee_id = 'emp_001'": csv_rows(
                "store_record_path",
                "/proc/stores/store_vienna_praterstern.json",
            ),
            "from stores order by length(store_name) desc": csv_rows(
                "store_name,record_path",
                "PowerTool Vienna Praterstern,/proc/stores/store_vienna_praterstern.json",
            ),
        },
        existing_paths={
            "/proc/baskets/basket_004.json",
            "/proc/stores/store_vienna_praterstern.json",
        },
    )

    assert submission_refs(
        CompletionStub(
            task_type="discount",
            grounding_doc_refs=["/docs/security.md", "/docs/discounts.md"],
            grounding_row_refs=[
                "/proc/employees/emp_001.json",
                "/proc/baskets/basket_004.json",
            ],
        ),
        vm,
        task_text=(
            "Before applying this, verify that Philipp Lehmann is actually a "
            "manager at PowerTool Vienna Praterstern. They approved a discount "
            "for my basket basket_004."
        ),
    ) == [
        "/docs/security.md",
        "/docs/discounts.md",
        "/proc/stores/store_vienna_praterstern.json",
        "/proc/baskets/basket_004.json",
    ]
