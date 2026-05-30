from runtime_calls import default_runtime_timeout_ms, runtime_call


class TimeoutAwareMethod:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int | None]] = []

    def __call__(self, request: object, *, timeout_ms: int | None = None) -> object:
        self.calls.append((request, timeout_ms))
        return request


class LegacyMethod:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, request: object) -> object:
        self.calls.append(request)
        return request


def test_default_runtime_timeout_is_short(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_RUNTIME_TIMEOUT_MS", raising=False)
    assert default_runtime_timeout_ms() == 300


def test_runtime_timeout_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_TIMEOUT_MS", "0")
    assert default_runtime_timeout_ms() is None


def test_runtime_call_passes_default_timeout(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_RUNTIME_TIMEOUT_MS", raising=False)
    method = TimeoutAwareMethod()

    runtime_call(method, "request")

    assert method.calls == [("request", 300)]


def test_runtime_call_respects_explicit_timeout() -> None:
    method = TimeoutAwareMethod()

    runtime_call(method, "request", timeout_ms=75)

    assert method.calls == [("request", 75)]


def test_runtime_call_supports_legacy_fakes(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_RUNTIME_TIMEOUT_MS", raising=False)
    method = LegacyMethod()

    runtime_call(method, "request")

    assert method.calls == ["request"]
