from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_DECLARATION_RE = re.compile(
    r"(?m)^[ \t]*(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*"
    r"(?P<kind>theorem|lemma|def|abbrev|axiom|constant|opaque)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_'.]*)"
)
_STRUCTURE_RE = re.compile(
    r"(?m)^[ \t]*(?P<command>namespace|section|end)\b"
    r"(?:[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_'.]*))?"
)
_FORBIDDEN_PROOF_RE = re.compile(r"\b(sorryAx|sorry|admit)\b")


@dataclass(frozen=True)
class Declaration:
    kind: str
    name: str
    signature: str
    block: str


def _mask_comments_and_strings(source: str) -> str:
    result = list(source)
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        if block_depth:
            if source.startswith("/-", index):
                result[index : index + 2] = "  "
                block_depth += 1
                index += 2
                continue
            if source.startswith("-/", index):
                result[index : index + 2] = "  "
                block_depth -= 1
                index += 2
                continue
            if source[index] != "\n":
                result[index] = " "
            index += 1
            continue
        if in_string:
            if source[index] != "\n":
                result[index] = " "
            if escaped:
                escaped = False
            elif source[index] == "\\":
                escaped = True
            elif source[index] == '"':
                in_string = False
            index += 1
            continue
        if source.startswith("--", index):
            end = source.find("\n", index)
            end = len(source) if end < 0 else end
            result[index:end] = " " * (end - index)
            index = end
            continue
        if source.startswith("/-", index):
            result[index : index + 2] = "  "
            block_depth = 1
            index += 2
            continue
        if source[index] == '"':
            result[index] = " "
            in_string = True
        index += 1
    return "".join(result)


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _qualified_names(masked: str, matches: list[re.Match[str]]) -> list[str]:
    structures = list(_STRUCTURE_RE.finditer(masked))
    structure_index = 0
    scopes: list[tuple[str, str | None]] = []
    names: list[str] = []
    for declaration in matches:
        while (
            structure_index < len(structures)
            and structures[structure_index].start() < declaration.start()
        ):
            structure = structures[structure_index]
            command = structure.group("command")
            if command in {"namespace", "section"}:
                scopes.append((command, structure.group("name")))
            elif scopes:
                scopes.pop()
            structure_index += 1

        name = declaration.group("name")
        if name.startswith("_root_."):
            names.append(name.removeprefix("_root_."))
            continue
        namespaces = [
            scope_name
            for scope_kind, scope_name in scopes
            if scope_kind == "namespace" and scope_name
        ]
        names.append(".".join([*namespaces, name]))
    return names


def declarations(source: str) -> list[Declaration]:
    masked = _mask_comments_and_strings(source)
    matches = list(_DECLARATION_RE.finditer(masked))
    qualified_names = _qualified_names(masked, matches)
    rows: list[Declaration] = []
    for position, match in enumerate(matches):
        start = match.start()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(source)
        assignment = masked.find(":=", match.end(), end)
        signature_end = assignment if assignment >= 0 else end
        rows.append(
            Declaration(
                kind=match.group("kind"),
                name=qualified_names[position],
                signature=_normalize(source[start:signature_end]),
                block=source[start:end].strip(),
            )
        )
    return rows


def _find_declaration(rows: list[Declaration], name: str) -> Declaration | None:
    if "." in name:
        matches = [row for row in rows if row.name == name]
    else:
        matches = [row for row in rows if row.name.rsplit(".", 1)[-1] == name]
    return matches[0] if len(matches) == 1 else None


def _find_exact_declaration(rows: list[Declaration], name: str) -> Declaration | None:
    matches = [row for row in rows if row.name == name]
    return matches[0] if len(matches) == 1 else None


def audit_source(
    baseline: str,
    candidate: str,
    *,
    final: bool = False,
    protect_existing_statements: bool = True,
    protected_declarations: list[str] | None = None,
    required_declaration: str | None = None,
    required_declaration_names: list[str] | None = None,
) -> dict[str, Any]:
    baseline_rows = declarations(baseline)
    candidate_rows = declarations(candidate)
    violations: list[str] = []

    if protect_existing_statements:
        for row in baseline_rows:
            if row.kind not in {"theorem", "lemma"}:
                continue
            current = _find_exact_declaration(candidate_rows, row.name)
            if current is None:
                violations.append(f"Protected declaration was removed: {row.name}")
            elif current.kind != row.kind or current.signature != row.signature:
                violations.append(f"Protected declaration statement changed: {row.name}")

    for name in protected_declarations or []:
        original = _find_declaration(baseline_rows, name)
        current = _find_declaration(candidate_rows, name)
        if original is None:
            violations.append(f"Explicitly protected declaration not found in baseline: {name}")
        elif current is None or current.block != original.block:
            violations.append(f"Explicitly protected declaration changed: {name}")

    required_name = None
    if required_declaration:
        required_rows = declarations(required_declaration + " := by trivial\n")
        if len(required_rows) != 1:
            violations.append("Formal goal contract could not be parsed deterministically")
        else:
            required = required_rows[0]
            required_name = required.name
            current = _find_declaration(candidate_rows, required.name)
            if current is None:
                violations.append(f"Formal goal declaration is missing: {required.name}")
            elif current.kind != required.kind or current.signature != required.signature:
                violations.append(f"Formal goal declaration changed: {required.name}")

    required_names = list(dict.fromkeys(required_declaration_names or []))
    for name in required_names:
        if _find_declaration(candidate_rows, name) is None:
            violations.append(f"Required Plan declaration is missing: {name}")

    baseline_unsafe = {
        row.name for row in baseline_rows if row.kind in {"axiom", "constant", "opaque"}
    }
    for row in candidate_rows:
        if row.kind in {"axiom", "constant", "opaque"} and row.name not in baseline_unsafe:
            violations.append(f"New {row.kind} declaration is forbidden: {row.name}")

    baseline_masked = _mask_comments_and_strings(baseline)
    candidate_masked = _mask_comments_and_strings(candidate)
    baseline_placeholders = len(_FORBIDDEN_PROOF_RE.findall(baseline_masked))
    candidate_placeholders = len(_FORBIDDEN_PROOF_RE.findall(candidate_masked))
    if final and candidate_placeholders:
        violations.append("Final source contains sorry, admit, or sorryAx")
    elif candidate_placeholders > baseline_placeholders:
        violations.append("Candidate introduced a new sorry, admit, or sorryAx")

    return {
        "ok": not violations,
        "final": final,
        "protect_existing_statements": protect_existing_statements,
        "protected_declarations": list(protected_declarations or []),
        "required_declaration": required_name,
        "required_declaration_names": required_names,
        "baseline_declarations": len(baseline_rows),
        "candidate_declarations": len(candidate_rows),
        "baseline_placeholders": baseline_placeholders,
        "candidate_placeholders": candidate_placeholders,
        "violations": violations,
    }
