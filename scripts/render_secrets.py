#!/usr/bin/env python3
"""Render the vK8s Secret manifests from deploy/vk8s/secrets/*.template.yaml.

Usage:
    uv run python scripts/render_secrets.py               # writes deploy/vk8s/secrets/rendered/<name>.yaml
    uv run python scripts/render_secrets.py --root PATH   # work on another checkout (tests use a tmp dir)

Each `{{ NAME }}` placeholder is filled like this (scripts/README.md):
    ALL_CLIENT_SECRETS_JSON  CLIENT_SECRETS_JSON from .secrets/secrets.env
    IDP_PRIVATE_KEY_PEM      the content of .secrets/idp-signing-key.pem
    anything else            .secrets/secrets.env, then .env (an empty value counts as missing)

How a value is written depends on where its placeholder sits:
    alone on its line        block-scalar content: every line of the value gets the placeholder's indentation
    inside "..."             escaped for a YAML double-quoted scalar (JSON string escaping)
    inside '...'             escaped for a YAML single-quoted scalar ('')
    anywhere else            inserted as is (must be one line)

All or nothing: every template is rendered and re-parsed with PyYAML first. A missing value, a template that
does not parse after rendering, or a value that does not survive the round trip is an error, and then no file
is written. The output directory is 0700 and each file 0600. Values are never printed.

Exit codes: 0 ok, 1 nothing written (the messages say why).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml
from envfile import read_env_file

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path("deploy/vk8s/secrets")
OUTPUT_DIR = TEMPLATE_DIR / "rendered"
TEMPLATE_SUFFIX = ".template.yaml"
SECRETS_ENV = Path(".secrets/secrets.env")
SIGNING_KEY = Path(".secrets/idp-signing-key.pem")
DOTENV = Path(".env")

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
ALL_SECRETS_PLACEHOLDER = "ALL_CLIENT_SECRETS_JSON"
ALL_SECRETS_NAME = "CLIENT_SECRETS_JSON"
PEM_PLACEHOLDER = "IDP_PRIVATE_KEY_PEM"
# gen_keys copies these from .env into secrets.env; if they now differ, .env was edited after gen_keys ran.
SYNCED_FROM_DOTENV = ("GRANITE_SUPPLY_SECRET", "GATEWAY_API_KEY")


class RenderError(Exception):
    """One problem with one template. Never contains a secret value."""


# ---- values -------------------------------------------------------------------------------------------------------


def load_values(root: Path) -> tuple[dict[str, str], dict[str, str], str | None]:
    """(secrets.env, .env, PEM text or None)."""
    pem_path = root / SIGNING_KEY
    pem = pem_path.read_text(encoding="utf-8") if pem_path.is_file() else None
    return read_env_file(root / SECRETS_ENV), read_env_file(root / DOTENV), pem


def lookup(name: str, generated: dict[str, str], dotenv: dict[str, str], pem: str | None) -> str | None:
    """The value for one placeholder, or None when it has none."""
    if name == PEM_PLACEHOLDER:
        return pem if pem and pem.strip() else None
    if name == ALL_SECRETS_PLACEHOLDER:
        return generated.get(ALL_SECRETS_NAME) or None
    return generated.get(name) or dotenv.get(name) or None


def drift_problems(generated: dict[str, str], dotenv: dict[str, str]) -> list[str]:
    """Names whose .env value no longer matches what gen_keys wrote (the cockpit would disagree with the Secret)."""
    return [
        f"{name} in .env differs from {SECRETS_ENV}; run scripts/gen_keys.py to sync it"
        for name in SYNCED_FROM_DOTENV
        if dotenv.get(name) and generated.get(name) and dotenv[name] != generated[name]
    ]


# ---- rendering ----------------------------------------------------------------------------------------------------


def open_quote(prefix: str) -> str | None:
    """The YAML quote character still open at the end of `prefix` (the start of one line), or None.

    A quote only opens a scalar where a value can start (line start, or after `: `, `- `, `[`, `{`, `,`),
    so the apostrophe in a plain scalar such as `note: it's {{ X }}` does not count."""
    quote = None
    i = 0
    while i < len(prefix):
        char = prefix[i]
        if quote == '"' and char == "\\":
            i += 2  # \" and friends: an escaped character
            continue
        if quote == "'" and prefix[i : i + 2] == "''":
            i += 2  # '' is an escaped single quote, not the end of the scalar
            continue
        if quote is None and char in "\"'" and starts_value(prefix[:i]):
            quote = char
        elif char == quote:
            quote = None
        i += 1
    return quote


def starts_value(before: str) -> bool:
    text = before.rstrip()
    return text == "" or text.endswith((":", "-", "[", "{", ",", "?"))


def escape_inline(value: str, quote: str | None, name: str) -> str:
    if quote == '"':
        return json.dumps(value)[1:-1]  # a JSON string body is a valid YAML double-quoted body
    if quote == "'":
        if "\n" in value:
            raise RenderError(f"{name} has several lines and cannot go in a single-quoted scalar")
        return value.replace("'", "''")
    if "\n" in value:
        raise RenderError(f"{name} has several lines; put its placeholder alone on a block-scalar line")
    return value


def render_line(line: str, values: dict[str, str]) -> str:
    """One template line with its placeholders filled. `values` holds every placeholder name it uses."""
    alone = PLACEHOLDER.fullmatch(line.strip())
    if alone:
        indent = line[: len(line) - len(line.lstrip())]
        content = values[alone.group(1)].rstrip("\n")
        return "\n".join(indent + value_line if value_line else "" for value_line in content.split("\n"))
    out, position = [], 0
    for match in PLACEHOLDER.finditer(line):
        out.append(line[position : match.start()])
        out.append(escape_inline(values[match.group(1)], open_quote(line[: match.start()]), match.group(1)))
        position = match.end()
    out.append(line[position:])
    return "".join(out)


def render_text(template: str, values: dict[str, str]) -> str:
    rendered = [render_line(line, values) for line in template.split("\n")]
    return "\n".join(rendered)


def string_values(node) -> list[str]:
    """Every string scalar in a parsed YAML document, recursively."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for key, value in node.items() for s in string_values(key) + string_values(value)]
    if isinstance(node, list):
        return [s for item in node for s in string_values(item)]
    return []


def check_parses(rendered: str, used: dict[str, str]) -> None:
    """The output must be valid YAML, and each value must come back out of it unchanged."""
    try:
        documents = list(yaml.safe_load_all(rendered))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        raise RenderError(f"rendered YAML does not parse{where}") from None
    strings = [s for document in documents for s in string_values(document)]
    for name, value in used.items():
        if not any(value.rstrip("\n") in s for s in strings):
            raise RenderError(f"{name} did not survive the YAML round trip (check its placeholder's quoting)")


def render_template(template: str, generated, dotenv, pem) -> tuple[str | None, list[str]]:
    """(rendered text or None, problems)."""
    names = sorted(set(PLACEHOLDER.findall(template)))
    used = {name: lookup(name, generated, dotenv, pem) for name in names}
    missing = [name for name, value in used.items() if value is None]
    if missing:
        return None, [f"no value for {name}" for name in missing]
    try:
        rendered = render_text(template, used)
        check_parses(rendered, used)
    except RenderError as e:
        return None, [str(e)]
    return rendered, []


def output_name(template_path: Path) -> str:
    return template_path.name.removesuffix(TEMPLATE_SUFFIX) + ".yaml"


def render_all(root: Path) -> tuple[dict[Path, str], list[str]]:
    """({output path: text}, problems). Writes nothing."""
    templates = sorted((root / TEMPLATE_DIR).glob(f"*{TEMPLATE_SUFFIX}"))
    if not templates:
        return {}, [f"no *{TEMPLATE_SUFFIX} files in {TEMPLATE_DIR}"]
    generated, dotenv, pem = load_values(root)
    outputs: dict[Path, str] = {}
    problems = drift_problems(generated, dotenv)
    for template_path in templates:
        rendered, template_problems = render_template(template_path.read_text(encoding="utf-8"), generated, dotenv, pem)
        problems += [f"{template_path.name}: {problem}" for problem in template_problems]
        if rendered is not None:
            outputs[root / OUTPUT_DIR / output_name(template_path)] = rendered
    return outputs, problems


# ---- writing ------------------------------------------------------------------------------------------------------


def write_private(path: Path, text: str) -> None:
    """Write a 0600 file without ever having it readable by others."""
    temp = path.with_name(path.name + ".tmp")
    temp.unlink(missing_ok=True)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def write_outputs(root: Path, outputs: dict[Path, str]) -> None:
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    for path, text in outputs.items():
        write_private(path, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repo root to work in (default: this checkout)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    outputs, problems = render_all(root)
    if problems:
        for problem in problems:
            print(f"render_secrets: {problem}", file=sys.stderr)
        print("render_secrets: nothing written. Run scripts/gen_keys.py, or set the value in .env", file=sys.stderr)
        return 1
    write_outputs(root, outputs)
    for path in outputs:
        print(f"render_secrets: wrote {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
