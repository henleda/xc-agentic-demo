"""Read and write simple NAME=value env files (.env and .secrets/secrets.env). Shared by gen_keys and render_secrets.

Reading follows docker compose closely enough for this repo: blank lines and `#` lines are skipped, an optional
`export ` prefix is dropped, a value wrapped in quotes loses them, and an unquoted value loses a trailing
` # comment`. No variable interpolation. An empty value reads as "" and callers treat that as unset.
"""

import re
from pathlib import Path

INLINE_COMMENT = re.compile(r"\s+#")


def read_env_file(path: Path) -> dict[str, str]:
    """NAME -> value. A missing file is an empty dict."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(line)
        if parsed:
            name, value = parsed
            values[name] = value
    return values


def parse_env_line(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    name, _, raw_value = text.partition("=")
    name = name.removeprefix("export ").strip()
    if not name:
        return None
    return name, parse_value(raw_value)


def parse_value(raw: str) -> str:
    """`raw` is everything after the first `=`. Leading spaces matter: `NAME=   # note` is an empty value."""
    stripped = raw.strip()
    if stripped[:1] in ('"', "'"):
        closing = stripped.find(stripped[0], 1)
        if closing > 0:
            return stripped[1:closing]
    return INLINE_COMMENT.split(raw, maxsplit=1)[0].strip()


def format_env_file(values: dict[str, str], header: str) -> str:
    """One unquoted NAME=value line per entry, after a `#` header.

    Unquoted, because `docker run --env-file` keeps quotes literally. So a value must read back unchanged
    (no newline, no ` #`, no surrounding spaces or quotes) and hold no `$` (docker compose would expand it)."""
    lines = [f"# {line}" if line else "#" for line in header.splitlines()]
    for name, value in values.items():
        if not env_safe(value):
            raise ValueError(f"{name}: value cannot be written unquoted to an env file (newline, '$', ' #' or quotes)")
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def env_safe(value: str) -> bool:
    return "\n" not in value and "$" not in value and parse_value(value) == value
