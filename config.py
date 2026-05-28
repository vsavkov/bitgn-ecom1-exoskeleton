import os
import re
from pathlib import Path


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROJECT_ROOT = Path(__file__).resolve().parent


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> None:
    env_path = Path(path) if path is not None else _PROJECT_ROOT / ".env"
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
