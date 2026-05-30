import csv
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ReadRequest
from connectrpc.errors import ConnectError

from runtime_calls import runtime_read


class RuntimeVM(Protocol):
    def read(self, request: ReadRequest) -> Any: ...


@dataclass(frozen=True)
class Package:
    package_id: str
    from_store_id: str
    to_store_id: str
    due_time: int
    margin_cents: int


@dataclass(frozen=True)
class Lane:
    lane_id: str
    from_node: str
    to_node: str
    capacity: int
    eta: int
    cost_cents: int


@dataclass(frozen=True)
class RouteCandidate:
    lane_ids: tuple[str, ...]
    eta: int
    cost_cents: int


@dataclass(frozen=True)
class DispatchPlan:
    message: str
    refs_to_submit: list[str]
    package_count: int


def dispatch_wave_path_from_task(task_text: str) -> str:
    for token in _path_tokens(task_text):
        if token.startswith("/ops/dispatch/") and token.endswith(".md"):
            return token
    return ""


def plan_dispatch_wave(vm: RuntimeVM, wave_md_path: str) -> DispatchPlan | None:
    wave_md = _read_text(vm, wave_md_path)
    if not wave_md:
        return None

    packages_path = _manifest_value(wave_md, "Packages")
    lanes_path = _manifest_value(wave_md, "Lanes")
    if not packages_path or not lanes_path:
        return None

    packages = _parse_packages(_read_text(vm, packages_path))
    lanes = _parse_lanes(_read_text(vm, lanes_path))
    if not packages or not lanes:
        return None

    assignments = _solve_assignments(packages, lanes)
    if not assignments:
        return None

    return DispatchPlan(
        message=json.dumps({"assignments": assignments}, separators=(",", ":")),
        refs_to_submit=[wave_md_path, packages_path, lanes_path],
        package_count=len(packages),
    )


def _solve_assignments(
    packages: list[Package],
    lanes: list[Lane],
) -> list[dict[str, object]]:
    lane_by_id = {lane.lane_id: lane for lane in lanes}
    priority_packages = sorted(
        packages,
        key=lambda package: (
            package.due_time,
            -package.margin_cents,
            package.package_id,
        ),
    )
    priority_by_package = {
        package.package_id: priority
        for priority, package in enumerate(priority_packages, start=1)
    }

    candidate_sets: list[list[RouteCandidate]] = []
    for package in packages:
        candidates = _route_candidates(package, lanes)
        if not candidates:
            return []
        candidate_sets.append(candidates)

    best_score: tuple[int, int, int, int] | None = None
    best_routes: tuple[RouteCandidate, ...] | None = None
    for candidate_tuple in product(*candidate_sets):
        route_by_package = {
            package.package_id: route
            for package, route in zip(packages, candidate_tuple, strict=True)
        }
        arrivals = _arrival_times(priority_packages, route_by_package, lane_by_id)
        late_count = sum(
            1
            for package in packages
            if arrivals.get(package.package_id, package.due_time + 1) > package.due_time
        )
        lateness = sum(
            max(0, arrivals.get(package.package_id, package.due_time + 1) - package.due_time)
            for package in packages
        )
        total_cost = sum(route.cost_cents for route in candidate_tuple)
        total_eta = sum(route.eta for route in candidate_tuple)
        score = (late_count, lateness, total_cost, total_eta)
        if best_score is None or score < best_score:
            best_score = score
            best_routes = candidate_tuple

    if best_routes is None:
        return []

    best_route_by_package = {
        package.package_id: route
        for package, route in zip(packages, best_routes, strict=True)
    }
    return [
        {
            "package_id": package.package_id,
            "route": list(best_route_by_package[package.package_id].lane_ids),
            "priority": priority_by_package[package.package_id],
        }
        for package in priority_packages
    ]


def _route_candidates(package: Package, lanes: list[Lane]) -> list[RouteCandidate]:
    by_from: dict[str, list[Lane]] = {}
    for lane in lanes:
        by_from.setdefault(lane.from_node, []).append(lane)

    paths: list[list[Lane]] = []

    def visit(node: str, path: list[Lane], seen_nodes: set[str]) -> None:
        if len(path) > 5:
            return
        if node == package.to_store_id and path:
            paths.append(path.copy())
            return
        for lane in by_from.get(node, []):
            if lane.to_node in seen_nodes:
                continue
            path.append(lane)
            seen_nodes.add(lane.to_node)
            visit(lane.to_node, path, seen_nodes)
            seen_nodes.remove(lane.to_node)
            path.pop()

    visit(package.from_store_id, [], {package.from_store_id})
    candidates = [
        RouteCandidate(
            lane_ids=tuple(lane.lane_id for lane in path),
            eta=sum(lane.eta for lane in path),
            cost_cents=sum(lane.cost_cents for lane in path),
        )
        for path in paths
    ]
    candidates.sort(
        key=lambda route: (
            max(0, route.eta - package.due_time),
            route.cost_cents,
            route.eta,
            len(route.lane_ids),
        )
    )

    result: list[RouteCandidate] = []
    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        if candidate.lane_ids in seen:
            continue
        seen.add(candidate.lane_ids)
        result.append(candidate)
        if len(result) >= 4:
            break
    return result


def _arrival_times(
    priority_packages: list[Package],
    route_by_package: dict[str, RouteCandidate],
    lane_by_id: dict[str, Lane],
) -> dict[str, int]:
    lane_usage: dict[str, int] = {}
    arrivals: dict[str, int] = {}
    for package in priority_packages:
        route = route_by_package[package.package_id]
        arrival = 0
        for lane_id in route.lane_ids:
            lane = lane_by_id[lane_id]
            used = lane_usage.get(lane_id, 0)
            arrival += (used // max(lane.capacity, 1)) * lane.eta
            arrival += lane.eta
            lane_usage[lane_id] = used + 1
        arrivals[package.package_id] = arrival
    return arrivals


def _parse_packages(content: str) -> list[Package]:
    rows = csv.DictReader(io.StringIO(content), delimiter="\t")
    packages: list[Package] = []
    for row in rows:
        try:
            packages.append(
                Package(
                    package_id=str(row.get("package_id") or ""),
                    from_store_id=str(row.get("from_store_id") or ""),
                    to_store_id=str(row.get("to_store_id") or ""),
                    due_time=int(row.get("due_time") or 0),
                    margin_cents=int(row.get("margin_cents") or 0),
                )
            )
        except ValueError:
            continue
    return [package for package in packages if package.package_id]


def _parse_lanes(content: str) -> list[Lane]:
    rows = csv.DictReader(io.StringIO(content), delimiter="\t")
    lanes: list[Lane] = []
    for row in rows:
        try:
            lanes.append(
                Lane(
                    lane_id=str(row.get("lane_id") or ""),
                    from_node=str(row.get("from") or ""),
                    to_node=str(row.get("to") or ""),
                    capacity=int(row.get("capacity") or 1),
                    eta=int(row.get("eta") or 0),
                    cost_cents=int(row.get("cost_cents") or 0),
                )
            )
        except ValueError:
            continue
    return [lane for lane in lanes if lane.lane_id]


def _manifest_value(content: str, label: str) -> str:
    prefix = f"{label}:"
    for line in content.splitlines():
        if line.startswith(prefix):
            return line.partition(":")[2].strip()
    return ""


def _read_text(vm: RuntimeVM, path: str) -> str:
    try:
        result = runtime_read(
            vm,
            ReadRequest(path=path, number=False, start_line=0, end_line=0),
        )
    except (AttributeError, ConnectError):
        return ""
    return getattr(result, "content", "") or ""


def _path_tokens(task_text: str) -> Iterable[str]:
    token_chars: list[str] = []
    allowed = {"/", "-", "_", "."}
    for char in task_text:
        if char.isalnum() or char in allowed:
            token_chars.append(char)
        else:
            token_chars.append(" ")
    for token in "".join(token_chars).split():
        yield token.strip(".")
