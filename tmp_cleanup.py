from dataclasses import dataclass
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import DeleteRequest, FindRequest, ListRequest, NodeKind
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from runtime_calls import runtime_delete, runtime_list
from runtime_calls import runtime_call
from task_classifier import TaskClassification


class RuntimeVM(Protocol):
    def find(self, request: FindRequest) -> Any: ...

    def list(self, request: ListRequest) -> Any: ...

    def delete(self, request: DeleteRequest) -> Any: ...


@dataclass(frozen=True)
class TmpCleanupResult:
    deleted_paths: list[str]
    completed_steps_laconic: list[str]


def tmp_cleanup_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> TmpCleanupResult | None:
    if not classification.raw_file_mutation_intent:
        return None

    root = _target_tmp_path(classification.tmp_cleanup_path)
    if root is None:
        return None

    tmp_suffix_only = classification.tmp_cleanup_only_tmp_suffix
    try:
        files = sorted(_runtime_files_under(vm, root))
    except ConnectError:
        return None

    targets = [
        path
        for path in files
        if not tmp_suffix_only or path.rsplit("/", 1)[-1].endswith(".tmp")
    ]
    deleted_paths: list[str] = []
    for path in targets:
        try:
            runtime_delete(vm, DeleteRequest(path=path))
        except ConnectError as exc:
            if exc.code == Code.NOT_FOUND:
                continue
            raise
        deleted_paths.append(path)

    scope = "ending in .tmp" if tmp_suffix_only else "under the requested /tmp path"
    return TmpCleanupResult(
        deleted_paths=deleted_paths,
        completed_steps_laconic=[
            f"Inspected {root} for files {scope}.",
            f"Deleted {len(deleted_paths)} matching file(s) and left non-matching files untouched.",
            "Returned the deleted paths sorted alphabetically.",
        ],
    )


def _target_tmp_path(task_text: str) -> str | None:
    path = task_text.strip().rstrip(".,;:)")
    if not path.startswith("/tmp/"):
        return None
    return path


def _runtime_files_under(vm: RuntimeVM, root: str) -> list[str]:
    files = set(_runtime_files_from_find(vm, root))
    files.update(_runtime_files_from_list(vm, root))
    return sorted(files)


def _runtime_files_from_find(vm: RuntimeVM, root: str) -> list[str]:
    if not hasattr(vm, "find"):
        return []
    try:
        result = runtime_call(
            vm.find,
            FindRequest(
                root=root,
                kind=NodeKind.NODE_KIND_FILE,
                limit=1000,
            ),
        )
    except Exception:
        return []

    files: list[str] = []
    for entry in getattr(result, "entries", []) or []:
        path = _entry_path(root, entry)
        if path.startswith(f"{root.rstrip('/')}/") or path == root:
            files.append(path)
    return files


def _runtime_files_from_list(vm: RuntimeVM, root: str) -> list[str]:
    listing = runtime_list(vm, ListRequest(path=root))
    files: list[str] = []
    for entry in getattr(listing, "entries", []) or []:
        path = _entry_path(root, entry)
        kind = getattr(entry, "kind", NodeKind.NODE_KIND_UNSPECIFIED)
        if kind == NodeKind.NODE_KIND_DIR:
            files.extend(_runtime_files_under(vm, path))
        elif kind in {NodeKind.NODE_KIND_FILE, NodeKind.NODE_KIND_UNSPECIFIED}:
            files.append(path)
    return files


def _entry_path(root: str, entry: Any) -> str:
    path = getattr(entry, "path", "") or ""
    if path.startswith("/"):
        return path
    name = path or getattr(entry, "name", "")
    return f"{root.rstrip('/')}/{name}"
