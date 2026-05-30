from collections.abc import Sequence
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind

from task_classifier import TaskClassification
from tmp_cleanup import tmp_cleanup_preflight


class FakeVM:
    def __init__(
        self,
        entries_by_path: dict[str, Sequence[SimpleNamespace]],
        *,
        find_entries_by_root: dict[str, Sequence[SimpleNamespace]] | None = None,
    ) -> None:
        self.entries_by_path = entries_by_path
        self.find_entries_by_root = find_entries_by_root or {}
        self.deleted_paths: list[str] = []

    def find(self, request):
        return SimpleNamespace(entries=self.find_entries_by_root.get(request.root, []))

    def list(self, request):
        return SimpleNamespace(entries=self.entries_by_path.get(request.path, []))

    def delete(self, request):
        self.deleted_paths.append(request.path)
        return SimpleNamespace()


def _entry(path: str, kind: NodeKind) -> SimpleNamespace:
    return SimpleNamespace(path=path, name=path.rsplit("/", 1)[-1], kind=kind)


def test_tmp_cleanup_deletes_only_exact_tmp_suffix_when_requested() -> None:
    vm = FakeVM(
        {
            "/tmp/job": [
                _entry("/tmp/job/a.tmp", NodeKind.NODE_KIND_FILE),
                _entry("/tmp/job/b.log", NodeKind.NODE_KIND_FILE),
                _entry("/tmp/job/nested", NodeKind.NODE_KIND_DIR),
            ],
            "/tmp/job/nested": [
                _entry("/tmp/job/nested/c.tmp", NodeKind.NODE_KIND_FILE),
                _entry("/tmp/job/nested/tmp.log", NodeKind.NODE_KIND_FILE),
            ],
        }
    )

    result = tmp_cleanup_preflight(
        vm,
        TaskClassification(
            raw_file_mutation_intent=True,
            tmp_cleanup_path="/tmp/job",
            tmp_cleanup_only_tmp_suffix=True,
        ),
    )

    assert result is not None
    assert result.deleted_paths == ["/tmp/job/a.tmp", "/tmp/job/nested/c.tmp"]
    assert vm.deleted_paths == result.deleted_paths


def test_tmp_cleanup_deletes_all_files_under_requested_path() -> None:
    vm = FakeVM(
        {
            "/tmp/job": [
                _entry("/tmp/job/a.tmp", NodeKind.NODE_KIND_FILE),
                _entry("/tmp/job/b.log", NodeKind.NODE_KIND_FILE),
            ],
        }
    )

    result = tmp_cleanup_preflight(
        vm,
        TaskClassification(
            raw_file_mutation_intent=True,
            tmp_cleanup_path="/tmp/job",
        ),
    )

    assert result is not None
    assert result.deleted_paths == ["/tmp/job/a.tmp", "/tmp/job/b.log"]
    assert vm.deleted_paths == result.deleted_paths


def test_tmp_cleanup_prefixes_relative_entry_paths() -> None:
    vm = FakeVM(
        {
            "/tmp/job": [
                SimpleNamespace(
                    path="a.tmp",
                    name="a.tmp",
                    kind=NodeKind.NODE_KIND_FILE,
                ),
                SimpleNamespace(
                    path="nested",
                    name="nested",
                    kind=NodeKind.NODE_KIND_DIR,
                ),
            ],
            "/tmp/job/nested": [
                SimpleNamespace(
                    path="b.tmp",
                    name="b.tmp",
                    kind=NodeKind.NODE_KIND_FILE,
                ),
            ],
        }
    )

    result = tmp_cleanup_preflight(
        vm,
        TaskClassification(
            raw_file_mutation_intent=True,
            tmp_cleanup_path="/tmp/job",
            tmp_cleanup_only_tmp_suffix=True,
        ),
    )

    assert result is not None
    assert result.deleted_paths == ["/tmp/job/a.tmp", "/tmp/job/nested/b.tmp"]
    assert vm.deleted_paths == result.deleted_paths


def test_tmp_cleanup_unions_find_and_list_results() -> None:
    vm = FakeVM(
        {
            "/tmp/job": [
                _entry("/tmp/job/a.log", NodeKind.NODE_KIND_FILE),
            ],
        },
        find_entries_by_root={
            "/tmp/job": [
                _entry("/tmp/job/a.log", NodeKind.NODE_KIND_FILE),
                _entry("/tmp/job/b.log", NodeKind.NODE_KIND_FILE),
            ]
        },
    )

    result = tmp_cleanup_preflight(
        vm,
        TaskClassification(
            raw_file_mutation_intent=True,
            tmp_cleanup_path="/tmp/job",
        ),
    )

    assert result is not None
    assert result.deleted_paths == ["/tmp/job/a.log", "/tmp/job/b.log"]
    assert vm.deleted_paths == result.deleted_paths
