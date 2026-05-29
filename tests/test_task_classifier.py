from dataclasses import dataclass, field
from typing import Any

from task_classifier import TaskClassification, classify_task


@dataclass
class FakeContent:
    parsed: Any = None
    type: str = "output_text"


@dataclass
class FakeOutputItem:
    type: str = "message"
    content: list[FakeContent] = field(default_factory=list)


@dataclass
class FakeResponse:
    output_parsed: Any = None
    output: list[FakeOutputItem] = field(default_factory=list)


class FakeResponses:
    def __init__(self, *, raise_exc: Exception | None = None, payload: Any = None):
        self.raise_exc = raise_exc
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResponse(output_parsed=self.payload)


class FakeClient:
    def __init__(self, responses: Any):
        self.responses = responses


def test_classify_task_returns_parsed_output() -> None:
    client = FakeClient(
        FakeResponses(
            payload=TaskClassification(
                explicit_basket_id="",
                checkout_intent=True,
                basket_selector="newest",
            )
        )
    )

    result = classify_task(client, "use my newest basket and check it out")

    assert result.checkout_intent is True
    assert result.basket_selector == "newest"
    assert result.explicit_basket_id == ""


def test_classify_task_returns_empty_for_blank_text_without_call() -> None:
    fake_responses = FakeResponses(payload=None)
    client = FakeClient(fake_responses)

    result = classify_task(client, "   ")

    assert result == TaskClassification()
    assert fake_responses.calls == []


def test_classify_task_falls_back_to_empty_on_helper_error() -> None:
    client = FakeClient(FakeResponses(raise_exc=RuntimeError("network")))

    result = classify_task(client, "check out my newest basket")

    assert result == TaskClassification()


def test_classify_task_accepts_dict_payload() -> None:
    client = FakeClient(
        FakeResponses(
            payload={
                "explicit_basket_id": "basket_145",
                "checkout_intent": True,
                "basket_selector": "none",
            }
        )
    )

    result = classify_task(client, "please check out basket_145")

    assert result.explicit_basket_id == "basket_145"
    assert result.checkout_intent is True
    assert result.basket_selector == "none"


class StaticParseResponses:
    def __init__(self, response: FakeResponse):
        self._response = response

    def parse(self, **kwargs: Any) -> FakeResponse:
        return self._response


def test_classify_task_reads_nested_message_content() -> None:
    nested = FakeOutputItem(
        content=[
            FakeContent(
                parsed=TaskClassification(
                    explicit_basket_id="",
                    checkout_intent=False,
                    basket_selector="none",
                )
            )
        ]
    )
    static = StaticParseResponses(FakeResponse(output=[nested]))
    client = FakeClient(static)

    result = classify_task(client, "how many work jackets do we sell?")

    assert result.checkout_intent is False
    assert result.basket_selector == "none"
