"""A small, deterministic YAML writer.

Written rather than delegated to PyYAML for one reason: exact control of output. PyYAML
re-sorts keys, chooses quoting heuristically and wraps long lines, none of which is
stable enough to golden-test against. This writer preserves insertion order, quotes only
when it must, and never wraps.

It emits the block-style subset that Ansible playbooks, inventories and variable files
need. It is not a general YAML library.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["dump_yaml"]

_INDENT = "  "

# Scalars that must be quoted or YAML would read them as another type. The boolean set is
# YAML 1.1's, which is what Ansible still follows: unquoted "yes" is True.
_AMBIGUOUS = frozenset(
    {
        "true", "false", "yes", "no", "on", "off", "y", "n",
        "null", "none", "~", "",
    }
)
_NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./@+-]*$")
#: YAML indicator characters that change a scalar's meaning when they lead it.
_LEADING_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")


def _is_plain_safe_phrase(value: str) -> bool:
    """True for a human phrase that needs no quoting, e.g. an Ansible task name.

    Quoting everything would be safe but unidiomatic; these are the conditions under
    which a plain scalar cannot be misread.
    """
    if not value or value != value.strip():
        return False
    if value[0] in _LEADING_INDICATORS:
        return False
    return not any(marker in value for marker in (": ", " #", "\n", "\t")) and not value.endswith(":")


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        value = str(value)

    if value == "":
        return '""'
    # Jinja expressions must be quoted or YAML tries to parse "{{" as a flow mapping.
    if value.startswith("{{") or value.startswith("{%"):
        return f'"{_escape(value)}"'
    if value.lower() in _AMBIGUOUS or _NUMERIC.match(value):
        return f'"{value}"'
    if _PLAIN_SAFE.match(value) and "\n" not in value:
        return value
    if _is_plain_safe_phrase(value):
        return value
    return f'"{_escape(value)}"'


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _key(value: str) -> str:
    return value if _PLAIN_SAFE.match(value) else f'"{_escape(value)}"'


def _render(value: Any, indent: int) -> list[str]:
    pad = _INDENT * indent

    if isinstance(value, dict):
        if not value:
            return [f"{pad}{{}}"]
        lines: list[str] = []
        for key, item in value.items():
            rendered_key = f"{pad}{_key(str(key))}:"
            if isinstance(item, (dict, list)) and item:
                lines.append(rendered_key)
                lines.extend(_render(item, indent + 1))
            elif isinstance(item, dict):
                lines.append(f"{rendered_key} {{}}")
            elif isinstance(item, list):
                lines.append(f"{rendered_key} []")
            else:
                lines.append(f"{rendered_key} {_scalar(item)}")
        return lines

    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{pad}[]"]
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                nested = _render(item, indent + 1)
                # Hoist the first key onto the dash line, as Ansible files conventionally do.
                first = nested[0].lstrip()
                lines.append(f"{pad}- {first}")
                lines.extend(nested[1:])
            elif isinstance(item, (list, tuple)) and item:
                nested = _render(item, indent + 1)
                lines.append(f"{pad}-")
                lines.extend(nested)
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return lines

    return [f"{pad}{_scalar(value)}"]


def dump_yaml(data: Any, *, header: tuple[str, ...] = (), document_start: bool = True) -> str:
    """Render ``data`` as YAML. Key order is preserved exactly."""
    # An empty header line becomes a bare "#", never "# " with trailing whitespace,
    # which ansible-lint flags.
    lines = [f"# {line}" if line else "#" for line in header]
    if document_start:
        lines.append("---")
    lines.extend(_render(data, 0))
    return "\n".join(lines).rstrip("\n") + "\n"
