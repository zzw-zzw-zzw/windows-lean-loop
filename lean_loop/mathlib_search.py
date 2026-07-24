from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from lean_loop.mathlib_index import (
    indexed_module_exists,
    index_status,
    mathlib_fingerprint,
    mathlib_source_root,
    search_index,
    similar_indexed_modules,
)
from lean_loop.process_control import ProcessCancelled, ProcessControl, run_controlled_process
from lean_loop.retrieval_cache import RetrievalCache


_UNKNOWN_NAME_RE = re.compile(
    r"Unknown (?:constant|identifier) [`']([^`']+)[`']",
    re.IGNORECASE,
)
_MISSING_MODULE_RE = re.compile(
    r"of module ([A-Za-z0-9_.]+) does not exist",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\b")
_TASK_IDENTIFIER_RE = re.compile(r"`([^`]+)`|\b[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\b")
_IMPORT_LINE_RE = re.compile(r"^\s*import\s+([A-Za-z0-9_.]+)\s*$")
_PRELUDE_LINE_RE = re.compile(r"^\s*prelude\s*$")
_MAX_IMPORT_HEADER_LINES = 4096
_MAX_IMPORT_HEADER_CHARS = 262_144

_LEAN_NOISE = {
    "abbrev", "axiom", "by", "class", "constant", "def", "deriving", "else",
    "example", "extends", "fun", "if", "in", "inductive", "instance", "lemma",
    "let", "match", "namespace", "noncomputable", "opaque", "open", "private",
    "protected", " theorem", " theorem", "then", "theorem", "where", "with",
    "True", "False", "Prop", "Type", "Sort", "self", "this",
}


@dataclass(frozen=True)
class SearchHit:
    query: str
    module: str
    path: str
    line: int
    snippet: str
    name: str = ""
    kind: str = "source"
    match: str = "text"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _HeaderImport:
    line_index: int
    module: str
    start: int
    end: int


@dataclass(frozen=True)
class _ImportHeader:
    valid: bool
    imports: tuple[_HeaderImport, ...]
    prelude_index: int | None
    declaration_index: int
    reason: str | None = None


def _code_outside_comments(line: str, block_depth: int) -> tuple[str, int, bool]:
    """Mask Lean comments while preserving character offsets and nesting."""
    code = list(line)
    index = 0
    malformed = False
    while index < len(line):
        pair = line[index:index + 2]
        if block_depth:
            code[index] = " "
            if pair == "/-":
                if index + 1 < len(code):
                    code[index + 1] = " "
                block_depth += 1
                index += 2
                continue
            if pair == "-/":
                if index + 1 < len(code):
                    code[index + 1] = " "
                block_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if pair == "--":
            for comment_index in range(index, len(code)):
                if line[comment_index] not in "\r\n":
                    code[comment_index] = " "
            break
        if pair == "/-":
            code[index] = " "
            if index + 1 < len(code):
                code[index + 1] = " "
            block_depth = 1
            index += 2
            continue
        if pair == "-/":
            malformed = True
            break
        index += 1
    return "".join(code), block_depth, malformed


def _scan_import_header(source: str) -> _ImportHeader:
    """Recognize only real ``prelude``/``import`` commands before declarations."""
    lines = source.splitlines(keepends=True)
    imports: list[_HeaderImport] = []
    prelude_index: int | None = None
    block_depth = 0
    scanned_chars = 0

    for line_index, line in enumerate(lines):
        if (
            line_index >= _MAX_IMPORT_HEADER_LINES
            or scanned_chars + len(line) > _MAX_IMPORT_HEADER_CHARS
        ):
            return _ImportHeader(
                False,
                tuple(imports),
                prelude_index,
                line_index,
                "import_header_scan_limit_exceeded",
            )
        scanned_chars += len(line)
        code, block_depth, malformed = _code_outside_comments(line, block_depth)
        if malformed:
            return _ImportHeader(
                False,
                tuple(imports),
                prelude_index,
                line_index,
                "malformed_import_header_comment",
            )
        stripped = code.strip()
        if not stripped:
            continue

        if _PRELUDE_LINE_RE.fullmatch(code):
            if prelude_index is not None or imports or block_depth:
                return _ImportHeader(
                    False,
                    tuple(imports),
                    prelude_index,
                    line_index,
                    "unclassifiable_prelude_header",
                )
            prelude_index = line_index
            continue

        match = _IMPORT_LINE_RE.fullmatch(code)
        if match:
            start = next(
                index for index, character in enumerate(code)
                if not character.isspace()
            )
            end = max(
                index for index, character in enumerate(code)
                if not character.isspace()
            ) + 1
            if block_depth or line[start:end] != code[start:end]:
                return _ImportHeader(
                    False,
                    tuple(imports),
                    prelude_index,
                    line_index,
                    "unclassifiable_import_command",
                )
            imports.append(
                _HeaderImport(line_index, match.group(1), start, end)
            )
            continue

        if re.match(r"^(?:import|prelude)\b", stripped):
            return _ImportHeader(
                False,
                tuple(imports),
                prelude_index,
                line_index,
                "unclassifiable_import_header",
            )
        return _ImportHeader(
            True, tuple(imports), prelude_index, line_index
        )

    if block_depth:
        return _ImportHeader(
            False,
            tuple(imports),
            prelude_index,
            len(lines),
            "unterminated_import_header_comment",
        )
    return _ImportHeader(True, tuple(imports), prelude_index, len(lines))


def _module_for(root: Path, path: Path) -> str:
    relative = path.resolve().relative_to(root.resolve()).with_suffix("")
    return "Mathlib." + ".".join(relative.parts)


def _parse_rg_json(root: Path, query: str, output: str, limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for raw in output.splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "match":
            continue
        data = row.get("data", {})
        path_text = data.get("path", {}).get("text")
        line_text = data.get("lines", {}).get("text", "").strip()
        line_number = data.get("line_number")
        if not isinstance(path_text, str) or not isinstance(line_number, int):
            continue
        path = Path(path_text)
        if not path.is_absolute():
            path = root / path
        hits.append(
            SearchHit(
                query=query,
                module=_module_for(root, path),
                path=str(path),
                line=line_number,
                snippet=line_text[:500],
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _search_with_rg(
    root: Path,
    query: str,
    limit: int,
    process_control: ProcessControl | None,
) -> list[SearchHit]:
    try:
        completed = run_controlled_process(
            [
                "rg",
                "--json",
                "--fixed-strings",
                "--glob",
                "*.lean",
                "--max-count",
                str(limit),
                query,
                str(root),
            ],
            timeout_seconds=60,
            kind="mathlib_search",
            control=process_control,
        )
    except subprocess.TimeoutExpired:
        return []
    if completed.returncode not in {0, 1}:
        return []
    return _parse_rg_json(root, query, completed.stdout, limit)


def _search_with_python(
    root: Path,
    query: str,
    limit: int,
    process_control: ProcessControl | None,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for path in root.rglob("*.lean"):
        if process_control is not None and process_control.cancel_requested():
            raise ProcessCancelled("Cancelled during Python Mathlib search")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, 1):
            if query in line:
                hits.append(
                    SearchHit(
                        query=query,
                        module=_module_for(root, path),
                        path=str(path),
                        line=line_number,
                        snippet=line.strip()[:500],
                    )
                )
                if len(hits) >= limit:
                    return hits
    return hits


def search_mathlib(
    project: Path,
    query: str,
    limit: int = 10,
    *,
    process_control: ProcessControl | None = None,
) -> list[SearchHit]:
    query = query.strip()
    if not query:
        return []
    root = mathlib_source_root(project)
    if index_status(project).get("valid"):
        return [
            SearchHit(
                query=hit.query,
                module=hit.module,
                path=hit.path,
                line=hit.line,
                snippet=hit.snippet,
                name=hit.name,
                kind=hit.kind,
                match=hit.match,
            )
            for hit in search_index(project, query, limit)
        ]
    if shutil.which("rg"):
        return _search_with_rg(root, query, limit, process_control)
    return _search_with_python(root, query, limit, process_control)


def suggest_imports(hits: Iterable[SearchHit | dict[str, object]]) -> list[dict[str, object]]:
    rows = [hit.to_dict() if isinstance(hit, SearchHit) else hit for hit in hits]
    indexed_evidence = any(str(row.get("match", "text")) != "text" for row in rows)
    modules: dict[str, dict[str, object]] = {}
    for row in rows:
        match = str(row.get("match", "text"))
        if indexed_evidence and match not in {"exact", "suffix"}:
            continue
        module = str(row.get("module", ""))
        if str(row.get("kind", "")) == "syntax":
            source_path = Path(str(row.get("path", "")))
            parent_module_source = source_path.parent.with_suffix(".lean")
            if parent_module_source.is_file() and ".Tactic." in module:
                module = module.rsplit(".", 1)[0]
        if not module or module == "Mathlib" or module.startswith("Mathlib.Init"):
            continue
        query = str(row.get("query", ""))
        suggestion = modules.setdefault(
            module,
            {
                "module": module,
                "queries": [],
                "evidence": [],
                "confidence": "candidate",
            },
        )
        if query and query not in suggestion["queries"]:
            suggestion["queries"].append(query)
        evidence = f"{row.get('path')}:{row.get('line')} {row.get('snippet')}"
        if evidence not in suggestion["evidence"] and len(suggestion["evidence"]) < 3:
            suggestion["evidence"].append(evidence)
        if match == "exact":
            suggestion["confidence"] = "high"
    return sorted(
        modules.values(),
        key=lambda row: (
            0 if row["confidence"] == "high" else 1,
            -len(row["queries"]),
            row["module"],
        ),
    )


def diagnostic_queries(diagnostics: str) -> list[str]:
    queries: list[str] = []
    for name in _UNKNOWN_NAME_RE.findall(diagnostics):
        token = name.rsplit(".", 1)[-1]
        if token and token not in queries:
            queries.append(token)
    return queries


def missing_modules(diagnostics: str) -> list[str]:
    return list(dict.fromkeys(_MISSING_MODULE_RE.findall(diagnostics)))


def source_search_terms(
    source: str,
    task: str = "",
    *,
    max_terms: int = 16,
) -> list[str]:
    """Extract likely external declarations without requiring a compiler run.

    This deliberately favors qualified names and names used in the task. Local
    binders are harmless search misses; the persistent index makes these cheap,
    while this catches proof repairs whose diagnostics contain no unknown name
    (notably files containing ``sorry`` or a broad ``import Mathlib``).
    """
    source_without_imports = "\n".join(
        line for line in source.splitlines() if not _IMPORT_LINE_RE.match(line)
    )
    source_terms = _IDENTIFIER_RE.findall(source_without_imports)
    task_terms = []
    for match in _TASK_IDENTIFIER_RE.finditer(task):
        value = match.group(1) or match.group(0)
        # Ordinary prose ("repair the proof") is not useful index evidence;
        # preserve explicit backtick names and Lean-shaped identifiers only.
        if (
            match.group(1)
            or "." in value
            or "_" in value
            or value[:1].isupper()
        ):
            task_terms.append(value)
    ordered: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if (
            not value
            or value in _LEAN_NOISE
            or value.lower() in {item.lower() for item in _LEAN_NOISE}
            or value.isdigit()
            or value.startswith("_")
            or value in ordered
        ):
            return
        ordered.append(value)

    # User/task names and qualified constants are the strongest evidence.
    for value in task_terms:
        add(value)
    for value in source_terms:
        if "." in value:
            add(value)
    for value in source_terms:
        if "_" in value or value[:1].isupper():
            add(value)
    for value in source_terms:
        add(value)
    return ordered[:max_terms]


def _newline_style(source: str) -> str:
    if "\r\n" in source:
        return "\r\n"
    if "\n" in source:
        return "\n"
    if "\r" in source:
        return "\r"
    return "\n"


def ensure_broad_mathlib_import(source: str) -> str:
    """Add one standalone ``import Mathlib`` to the existing Lean header."""
    header = _scan_import_header(source)
    if not header.valid:
        raise ValueError(
            f"Cannot safely classify Lean import header: {header.reason}"
        )
    if any(command.module == "Mathlib" for command in header.imports):
        return source

    lines = source.splitlines(keepends=True)
    if header.imports:
        insertion_index = header.imports[-1].line_index + 1
    else:
        insertion_index = (
            header.prelude_index + 1
            if header.prelude_index is not None
            else header.declaration_index
        )

    newline = _newline_style(source)
    if insertion_index > 0 and not lines[insertion_index - 1].endswith(("\n", "\r")):
        lines[insertion_index - 1] += newline
    has_following_source = insertion_index < len(lines)
    preserve_trailing_newline = source.endswith(("\n", "\r"))
    ending = newline if has_following_source or preserve_trailing_newline else ""
    lines.insert(insertion_index, f"import Mathlib{ending}")
    return "".join(lines)


def optimize_broad_imports(
    source: str,
    retrieval: dict[str, object],
    *,
    max_imports: int = 12,
) -> tuple[str, dict[str, object]]:
    """Replace only a standalone ``import Mathlib`` using exact local evidence.

    The returned source is a probe candidate. The workflow must compile it and
    keep the original source when the probe fails. Low-confidence or Init
    modules are intentionally ignored because they are poor replacement roots.
    """
    suggestions = retrieval.get("import_suggestions", [])
    selected_modules: list[str] = []
    if isinstance(suggestions, list):
        for row in suggestions:
            if not isinstance(row, dict):
                continue
            module = str(row.get("module", "")).strip()
            if (
                not module
                or module == "Mathlib"
                or module.startswith("Mathlib.Init")
                or str(row.get("confidence", "")) != "high"
                or module in selected_modules
            ):
                continue
            selected_modules.append(module)
            if len(selected_modules) >= max_imports:
                break

    header = _scan_import_header(source)
    if not header.valid:
        return source, {
            "changed": False,
            "reason": header.reason or "unclassifiable_import_header",
            "selected_modules": selected_modules,
            "added_modules": [],
        }

    lines = source.splitlines(keepends=True)
    broad_imports = [
        command for command in header.imports if command.module == "Mathlib"
    ]
    existing_modules = {
        command.module
        for command in header.imports
        if command.module != "Mathlib"
    }
    added_modules = [
        module for module in selected_modules if module not in existing_modules
    ]
    if not broad_imports:
        return source, {
            "changed": False,
            "reason": "no_broad_import",
            "selected_modules": selected_modules,
            "added_modules": added_modules,
        }
    if not selected_modules:
        return source, {
            "changed": False,
            "reason": "no_high_confidence_imports",
            "broad_import_count": len(broad_imports),
            "selected_modules": [],
            "added_modules": [],
        }

    first = broad_imports[0]
    replacement = _newline_style(source).join(
        f"import {module}" for module in added_modules
    )
    edits = {
        command.line_index: (
            command,
            replacement if command == first else "",
        )
        for command in broad_imports
    }
    optimized_lines: list[str] = []
    for line_index, line in enumerate(lines):
        edit = edits.get(line_index)
        if edit is None:
            optimized_lines.append(line)
            continue
        command, command_replacement = edit
        updated = (
            line[:command.start]
            + command_replacement
            + line[command.end:]
        )
        if command_replacement or updated.strip():
            optimized_lines.append(updated)
    optimized = "".join(optimized_lines)
    return optimized, {
        "changed": optimized != source,
        "reason": (
            "high_confidence_index_evidence"
            if added_modules
            else "high_confidence_remove_only"
        ),
        "replaced": "import Mathlib",
        "modules": selected_modules,
        "selected_modules": selected_modules,
        "added_modules": added_modules,
        "broad_import_count": len(broad_imports),
    }


def has_broad_import(source: str) -> bool:
    """Return whether the source contains a standalone ``import Mathlib``."""
    header = _scan_import_header(source)
    return header.valid and any(
        command.module == "Mathlib" for command in header.imports
    )


def validate_mathlib_imports(project: Path, source: str) -> dict[str, object]:
    modules = list(
        dict.fromkeys(
            match.group(1)
            for line in source.splitlines()
            if (match := _IMPORT_LINE_RE.match(line))
            and match.group(1).startswith("Mathlib.")
        )
    )
    invalid: list[dict[str, object]] = []
    root = mathlib_source_root(project)
    indexed = bool(index_status(project).get("valid"))
    for module in modules:
        source_path = root / (
            module.removeprefix("Mathlib.").replace(".", "/") + ".lean"
        )
        exists = indexed_module_exists(project, module) if indexed else source_path.is_file()
        if exists:
            continue
        suggestions = (
            similar_indexed_modules(project, module)
            if indexed
            else []
        )
        invalid.append(
            {
                "module": module,
                "source_path": str(source_path),
                "suggestions": suggestions,
            }
        )
    return {"ok": not invalid, "modules": modules, "invalid": invalid}


def import_validation_diagnostics(validation: dict[str, object]) -> str:
    invalid = validation.get("invalid", [])
    if not isinstance(invalid, list) or not invalid:
        return ""
    lines = ["Deterministic import validation failed before Lean check:"]
    for row in invalid:
        if not isinstance(row, dict):
            continue
        lines.append(f"- Mathlib module does not exist: {row.get('module')}")
        suggestions = row.get("suggestions", [])
        if isinstance(suggestions, list) and suggestions:
            names = [
                str(item.get("module"))
                for item in suggestions
                if isinstance(item, dict) and item.get("module")
            ]
            if names:
                lines.append("  Similar local modules: " + ", ".join(names))
    lines.append("Do not run lake build for a module whose source path does not exist.")
    return "\n".join(lines)


def repair_invalid_mathlib_imports(
    project: Path,
    source: str,
    *,
    minimum_score: float = 0.95,
    minimum_margin: float = 0.05,
) -> tuple[str, dict[str, object]]:
    validation = validate_mathlib_imports(project, source)
    replacements: dict[str, str] = {}
    for row in validation.get("invalid", []):
        if not isinstance(row, dict):
            continue
        suggestions = row.get("suggestions", [])
        if not isinstance(suggestions, list) or not suggestions:
            continue
        first = suggestions[0] if isinstance(suggestions[0], dict) else {}
        second = (
            suggestions[1]
            if len(suggestions) > 1 and isinstance(suggestions[1], dict)
            else {}
        )
        score = float(first.get("score") or 0)
        second_score = float(second.get("score") or 0)
        replacement = str(first.get("module") or "")
        original = str(row.get("module") or "")
        if (
            original
            and replacement
            and score >= minimum_score
            and score - second_score >= minimum_margin
        ):
            replacements[original] = replacement
    if not replacements:
        return source, {
            "changed": False,
            "reason": "no_unambiguous_local_module_repair",
            "validation": validation,
            "replacements": {},
        }
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _IMPORT_LINE_RE.match(line)
        if match and match.group(1) in replacements:
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"import {replacements[match.group(1)]}{ending}"
    repaired = "".join(lines)
    repaired_validation = validate_mathlib_imports(project, repaired)
    return repaired, {
        "changed": repaired != source,
        "reason": "unambiguous_local_module_repair",
        "replacements": replacements,
        "original_validation": validation,
        "validation": repaired_validation,
    }


def collect_retrieval(
    project: Path,
    *,
    diagnostics: str,
    requested_terms: Iterable[str] = (),
    per_query_limit: int = 5,
    max_queries: int = 8,
    process_control: ProcessControl | None = None,
) -> dict[str, object]:
    queries = diagnostic_queries(diagnostics)
    for term in requested_terms:
        clean = str(term).strip()
        if clean and clean not in queries:
            queries.append(clean)
    queries = queries[:max_queries]

    fingerprint = mathlib_fingerprint(project)
    status = index_status(project)
    backend = "sqlite-index" if status.get("valid") else (
        "ripgrep" if shutil.which("rg") else "python-scan"
    )
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "queries": queries,
                "missing_modules": missing_modules(diagnostics),
                "per_query_limit": per_query_limit,
                "max_queries": max_queries,
                "backend": backend,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    cache = RetrievalCache(project)
    cached = cache.get(cache_key, fingerprint)
    if cached is not None:
        cached["cache"] = {"hit": True, "key": cache_key}
        return cached

    hits: list[SearchHit] = []
    for query in queries:
        hits.extend(
            search_mathlib(
                project,
                query,
                per_query_limit,
                process_control=process_control,
            )
        )

    root = mathlib_source_root(project)
    module_checks: list[dict[str, object]] = []
    for module in missing_modules(diagnostics):
        relative = module.removeprefix("Mathlib.").replace(".", "/") + ".lean"
        source = root / relative
        module_checks.append(
            {
                "module": module,
                "source_exists": source.is_file(),
                "source_path": str(source),
                "guidance": (
                    "Module source exists but its compiled object is unavailable. "
                    "Avoid adding this import unless the project builds it."
                    if source.is_file()
                    else "Module source does not exist. Do not import this module."
                ),
            }
        )

    result: dict[str, object] = {
        "queries": queries,
        "hits": [hit.to_dict() for hit in hits],
        "module_checks": module_checks,
        "import_suggestions": suggest_imports(hits),
        "search_backend": backend,
        "index_fingerprint": fingerprint,
    }
    cache.put(cache_key, fingerprint, result)
    result["cache"] = {"hit": False, "key": cache_key}
    return result


def retrieval_prompt_block(retrieval: dict[str, object]) -> str:
    hits = retrieval.get("hits", [])
    module_checks = retrieval.get("module_checks", [])
    import_suggestions = retrieval.get("import_suggestions", [])
    lsp_evidence = retrieval.get("lsp")
    if not hits and not module_checks and not import_suggestions and not lsp_evidence:
        return "No local Mathlib matches were found for this attempt."
    lines = ["Local Mathlib evidence (read-only, exact local files):"]
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        lines.append(
            f"- query={hit.get('query')!r}; import {hit.get('module')}; "
            f"{hit.get('path')}:{hit.get('line')}: {hit.get('snippet')}"
        )
    for check in module_checks:
        if isinstance(check, dict):
            lines.append(f"- module {check.get('module')}: {check.get('guidance')}")
    if import_suggestions:
        lines.append(
            "Precise import candidates from the local index (candidates only; Lean must verify sufficiency):"
        )
        for suggestion in import_suggestions:
            if isinstance(suggestion, dict):
                lines.append(
                    f"- import {suggestion.get('module')} covers "
                    f"{suggestion.get('queries')} confidence={suggestion.get('confidence')}"
                )
    if isinstance(lsp_evidence, dict):
        rendered = json.dumps(lsp_evidence, ensure_ascii=False, indent=2)
        if len(rendered) > 32_000:
            rendered = rendered[:32_000] + "\n... [LSP evidence truncated]"
        lines.extend(
            [
                "Lean LSP evidence (advisory; the deterministic lake check remains authoritative):",
                rendered,
            ]
        )
    return "\n".join(lines)
