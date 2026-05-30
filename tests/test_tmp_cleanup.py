from collections.abc import Sequence
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind

from task_classifier import TaskClassification
from tmp_cleanup import tmp_cleanup_preflight


class FakeVM:
    def __init__(self, entries_by_path: dict[str, Sequence[SimpleNamespace]]) -> None:
        self.entries_by_path = entries_by_path
        self.deleted_paths: list[str] = []

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
