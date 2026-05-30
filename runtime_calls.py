from collections.abc import Callable
from inspect import Parameter, signature
from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError

from config import env_int


def default_runtime_timeout_ms() -> int | None:
    value = env_int("AGENT_RUNTIME_TIMEOUT_MS", 300, minimum=0)
    return value or None


def runtime_retry_timeout_ms() -> int | None:
    value = env_int("AGENT_RUNTIME_RETRY_TIMEOUT_MS", 1500, minimum=0)
    return value or None


def runtime_call(
    method: Callable[..., Any],
    request: Any,
    *,
    timeout_ms: int | None = None,
) -> Any:
    effective_timeout_ms = (
        default_runtime_timeout_ms() if timeout_ms is None else timeout_ms
    )
    if effective_timeout_ms is None:
        return method(request)
    if not _accepts_timeout_ms(method):
        return method(request)
    try:
        return method(request, timeout_ms=effective_timeout_ms)
    except ConnectError as exc:
        retry_timeout_ms = runtime_retry_timeout_ms()
        if (
            timeout_ms is not None
            or exc.code != Code.DEADLINE_EXCEEDED
            or retry_timeout_ms is None
            or retry_timeout_ms <= effective_timeout_ms
        ):
            raise
        return method(request, timeout_ms=retry_timeout_ms)


def runtime_exec(vm: Any, request: Any, *, timeout_ms: int | None = None) -> Any:
    return runtime_call(vm.exec, request, timeout_ms=timeout_ms)


def runtime_list(vm: Any, request: Any, *, timeout_ms: int | None = None) -> Any:
    return runtime_call(vm.list, request, timeout_ms=timeout_ms)


def runtime_read(vm: Any, request: Any, *, timeout_ms: int | None = None) -> Any:
    return runtime_call(vm.read, request, timeout_ms=timeout_ms)


def runtime_delete(vm: Any, request: Any, *, timeout_ms: int | None = None) -> Any:
    return runtime_call(vm.delete, request, timeout_ms=timeout_ms)


def runtime_stat(vm: Any, request: Any, *, timeout_ms: int | None = None) -> Any:
    return runtime_call(vm.stat, request, timeout_ms=timeout_ms)


def _accepts_timeout_ms(method: Callable[..., Any]) -> bool:
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return True

    for parameter in parameters:
        if parameter.name == "timeout_ms":
            return True
        if parameter.kind == Parameter.VAR_KEYWORD:
            return True
    return False
