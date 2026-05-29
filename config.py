import os
import re
from pathlib import Path
from typing import cast

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from openai.types.shared_params import ReasoningEffort


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROJECT_ROOT = Path(__file__).resolve().parent
PROMPT_DIR = PROJECT_ROOT / "prompts"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> None:
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            continue
        if not override and key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        print(f"{CLI_RED}Ignoring invalid {name}={raw_value!r}; using {default}{CLI_CLR}")
        return default

    return max(minimum, value)


def env_choice(name: str, default: str, choices: set[str]) -> str:
    raw_value = (os.getenv(name) or "").strip().lower()
    if not raw_value:
        return default

    if raw_value not in choices:
        print(f"{CLI_RED}Ignoring invalid {name}={raw_value!r}; using {default}{CLI_CLR}")
        return default
    return raw_value


def render_prompt(name: str) -> str:
    env = Environment(
        loader=FileSystemLoader(PROMPT_DIR),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    return env.get_template(name).render().strip()


def openai_client_kwargs() -> dict:
    return {
        "timeout": env_int("OPENAI_TIMEOUT_SECONDS", 40, minimum=1),
        "max_retries": env_int("OPENAI_MAX_RETRIES", 1, minimum=0),
    }


def helper_model() -> str:
    return os.getenv("HELPER_MODEL", "gpt-5.4-nano")


def helper_reasoning_effort() -> ReasoningEffort:
    return cast(
        ReasoningEffort,
        env_choice(
            "HELPER_REASONING_EFFORT",
            "low",
            {"none", "low", "medium", "high", "xhigh"},
        ),
    )
