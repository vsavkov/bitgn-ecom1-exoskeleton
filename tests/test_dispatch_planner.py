from types import SimpleNamespace

from dispatch_planner import dispatch_wave_path_from_task, plan_dispatch_wave


class FakeVM:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def read(self, request) -> object:
        return SimpleNamespace(content=self.files.get(request.path, ""))


def test_dispatch_wave_path_from_task_extracts_manifest_path() -> None:
    assert (
        dispatch_wave_path_from_task(
            "Plan /ops/dispatch/wave-ABC123/dispatch.md."
        )
        == "/ops/dispatch/wave-ABC123/dispatch.md"
    )
    assert dispatch_wave_path_from_task("Plan dispatch please") == ""


def test_plan_dispatch_wave_returns_compact_json_and_refs() -> None:
    vm = FakeVM(
        {
            "/ops/dispatch/wave-test/dispatch.md": (
                "# Dispatch Wave\n"
                "Packages: /ops/dispatch/wave-test/packages.tsv\n"
                "Lanes: /ops/dispatch/wave-test/lanes.tsv\n"
            ),
            "/ops/dispatch/wave-test/packages.tsv": (
                "package_id\tsku\tproduct_ref\tfrom_store_id\tfrom_store_ref\t"
                "to_store_id\tto_store_ref\tdue_time\tmargin_cents\treason\n"
                "XFER-001\tSKU-1\t/proc/catalog/SKU-1.json\tstore-a\t"
                "/proc/stores/store-a.json\tstore-c\t/proc/stores/store-c.json\t"
                "8\t1000\tneeded\n"
                "XFER-002\tSKU-2\t/proc/catalog/SKU-2.json\tstore-a\t"
                "/proc/stores/store-a.json\tstore-b\t/proc/stores/store-b.json\t"
                "3\t500\tneeded\n"
            ),
            "/ops/dispatch/wave-test/lanes.tsv": (
                "lane_id\tfrom\tto\tcapacity\teta\tcost_cents\tdelay_hint\n"
                "lane-a-b\tstore-a\tstore-b\t1\t2\t100\tdelays unlikely\n"
                "lane-b-c\tstore-b\tstore-c\t1\t2\t100\tdelays unlikely\n"
                "lane-a-c\tstore-a\tstore-c\t1\t7\t500\tdelays unlikely\n"
            ),
        }
    )

    plan = plan_dispatch_wave(vm, "/ops/dispatch/wave-test/dispatch.md")

    assert plan is not None
    assert plan.refs_to_submit == [
        "/ops/dispatch/wave-test/dispatch.md",
        "/ops/dispatch/wave-test/packages.tsv",
        "/ops/dispatch/wave-test/lanes.tsv",
    ]
    assert plan.message == (
        '{"assignments":[{"package_id":"XFER-002","route":["lane-a-b"],'
        '"priority":1},{"package_id":"XFER-001","route":["lane-a-b",'
        '"lane-b-c"],"priority":2}]}'
    )
