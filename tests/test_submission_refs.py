from dataclasses import dataclass, field

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from submission_refs import (
    EXPLICIT_RECORD_SPECS,
    can_auto_cite_customer_scoped_record,
    candidate_record_ids,
    canonical_proc_record_ref,
    dedupe_refs,
    explicit_target_refs_from_task,
    is_document_ref,
    normalize_runtime_path,
    normalize_submission_refs,
    parse_runtime_identity,
    split_ref_fragment,
    sql_quote,
    sql_record_path,
    sql_rows,
    submission_refs,
)


@dataclass
class CompletionStub:
    task_type: str = "other"
    protected_record_denial: bool = False
    grounding_doc_refs: list[str] = field(default_factory=list)
    grounding_row_refs: list[str] = field(default_factory=list)


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
        existing_paths: set[str] | None = None,
    ) -> None:
        self.id_stdout = id_stdout
        self.sql_outputs = sql_outputs or {}
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
            grounding_doc_refs=["/docs/security.md"],
            grounding_row_refs=["/proc/baskets/basket_001.json"],
        ),
        vm,
        task_text="basket_001",
    ) == ["/docs/security.md"]


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
