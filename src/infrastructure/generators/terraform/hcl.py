"""A small, deterministic HCL2 writer.

Terraform is generated programmatically rather than by string-formatting templates, for
three reasons: attribute quoting and escaping are handled in one place; block ordering is
explicit and stable; and the output is a pure function of its input, which is what the
golden tests rely on.

It writes the subset of HCL that infrastructure generation needs — blocks, nested blocks,
primitives, lists, maps and raw expression references — and nothing more. It is not a
general HCL library and does not try to be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Block", "HCLDocument", "Raw", "render_value"]

_INDENT = "  "


@dataclass(frozen=True, slots=True)
class Raw:
    """An HCL expression emitted verbatim.

    Used for references (``aws_vpc.main.id``), function calls and variable
    interpolations — anything that must not be quoted as a string literal.
    """

    expression: str

    def __str__(self) -> str:
        return self.expression


def _escape(text: str) -> str:
    """Escape a Python string for an HCL double-quoted literal.

    ``${`` is escaped to ``$${`` so that user-supplied text containing it is never
    interpreted as a Terraform interpolation.
    """
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return escaped.replace("${", "$${").replace("%{", "%%{")


def render_value(value: Any, indent: int = 0) -> str:
    """Render one HCL value. Collections keep their given order."""
    pad = _INDENT * indent

    if isinstance(value, Raw):
        return value.expression
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f'"{_escape(value)}"'
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = ",\n".join(
            f"{pad}{_INDENT}{render_value(item, indent + 1)}" for item in value
        )
        return f"[\n{items},\n{pad}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        # Align on the *rendered* key, since a key may end up quoted. Misaligned
        # output would fail `terraform fmt -check`.
        rendered_keys = {key: _render_key(key) for key in value}
        width = max(len(name) for name in rendered_keys.values())
        lines = "\n".join(
            f"{pad}{_INDENT}{rendered_keys[key]:<{width}} = {render_value(item, indent + 1)}"
            for key, item in value.items()
        )
        return f"{{\n{lines}\n{pad}}}"
    raise TypeError(f"cannot render {type(value).__name__} as HCL")


def _render_key(key: str) -> str:
    """Bare identifier where legal, quoted otherwise (e.g. tag keys with dashes)."""
    if key and (key[0].isalpha() or key[0] == "_") and all(
        char.isalnum() or char in "_-" for char in key
    ):
        return key if "-" not in key else f'"{key}"'
    return f'"{_escape(key)}"'


@dataclass(slots=True)
class Block:
    """An HCL block: ``type "label" "label" { ... }``."""

    type: str
    labels: tuple[str, ...] = ()
    #: Attribute order is preserved exactly as inserted — output stability matters more
    #: than alphabetical tidiness, because diffs are read by humans.
    attributes: dict[str, Any] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    #: Comment lines emitted immediately above the block.
    comments: tuple[str, ...] = ()

    def set(self, key: str, value: Any) -> Block:
        self.attributes[key] = value
        return self

    def set_if(self, key: str, value: Any) -> Block:
        """Set only when the value is meaningful. Keeps optional arguments out."""
        if value is not None and value != () and value != []:
            self.attributes[key] = value
        return self

    def add(self, block: Block) -> Block:
        self.blocks.append(block)
        return self

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        lines: list[str] = [f"{pad}# {comment}" for comment in self.comments]

        header = " ".join([self.type, *(f'"{label}"' for label in self.labels)])
        lines.append(f"{pad}{header} {{")

        if self.attributes:
            width = max(len(key) for key in self.attributes)
            for key, value in self.attributes.items():
                rendered = render_value(value, indent + 1)
                lines.append(f"{pad}{_INDENT}{key:<{width}} = {rendered}")

        for nested in self.blocks:
            if self.attributes or nested is not self.blocks[0]:
                lines.append("")
            lines.append(nested.render(indent + 1))

        lines.append(f"{pad}}}")
        return "\n".join(lines)


@dataclass(slots=True)
class HCLDocument:
    """A whole ``.tf`` file."""

    #: Header comment lines. No timestamps: identical input must give identical bytes.
    header: tuple[str, ...] = ()
    blocks: list[Block] = field(default_factory=list)

    def add(self, block: Block) -> HCLDocument:
        self.blocks.append(block)
        return self

    def extend(self, blocks: list[Block]) -> HCLDocument:
        self.blocks.extend(blocks)
        return self

    def render(self) -> str:
        parts: list[str] = []
        if self.header:
            parts.append("\n".join(f"# {line}" for line in self.header))
        parts.extend(block.render() for block in self.blocks)
        # Exactly one trailing newline, blocks separated by exactly one blank line:
        # this is what `terraform fmt -check` expects.
        return "\n\n".join(part for part in parts if part is not None).rstrip("\n") + "\n"

    @property
    def is_empty(self) -> bool:
        return not self.blocks
