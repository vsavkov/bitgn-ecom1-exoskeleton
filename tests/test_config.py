from pathlib import Path

from config import (
    env_choice,
    env_flag,
    env_flag_default,
    env_int,
    helper_model,
    helper_reasoning_effort,
    load_dotenv,
    openai_client_kwargs,
    render_prompt,
)


def test_env_helpers(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("FLAG_TRUE", " yes ")
    monkeypatch.setenv("COUNT", "12")
    monkeypatch.setenv("BAD_COUNT", "oops")
    monkeypatch.setenv("CHOICE", "HIGH")
    monkeypatch.setenv("BAD_CHOICE", "weird")
    monkeypatch.setenv("HELPER_MODEL", "custom-helper")
    monkeypatch.setenv("HELPER_REASONING_EFFORT", "medium")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "41")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "3")

    assert env_flag("FLAG_TRUE")
    assert not env_flag("MISSING_FLAG")
    assert env_flag_default("MISSING_DEFAULT_TRUE", True)
    assert not env_flag_default("MISSING_DEFAULT_FALSE", False)
    monkeypatch.setenv("FLAG_FALSE", "off")
    assert not env_flag_default("FLAG_FALSE", True)
    assert env_int("COUNT", 5, minimum=0) == 12
    assert env_int("COUNT", 5, minimum=20) == 20
    assert env_int("BAD_COUNT", 5) == 5
    assert env_choice("CHOICE", "low", {"low", "high"}) == "high"
    assert env_choice("BAD_CHOICE", "low", {"low", "high"}) == "low"
    assert helper_model() == "custom-helper"
    assert helper_reasoning_effort() == "medium"
    assert openai_client_kwargs() == {"timeout": 41, "max_retries": 3}
    assert "Ignoring invalid" in capsys.readouterr().out

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "export NEW_VALUE='new'",
                "EXISTING=from-file",
                "1INVALID=ignored",
                "NO_EQUALS",
            ]
        )
    )
    monkeypatch.setenv("EXISTING", "from-env")
    load_dotenv(env_file)
    assert helper_model() == "custom-helper"
    assert env_flag("MISSING_FLAG") is False
    assert __import__("os").environ["NEW_VALUE"] == "new"
    assert __import__("os").environ["EXISTING"] == "from-env"
    load_dotenv(env_file, override=True)
    assert __import__("os").environ["EXISTING"] == "from-file"


def test_render_prompt_reads_known_prompt() -> None:
    assert "pragmatic ecommerce operations assistant" in render_prompt("main.j2")
