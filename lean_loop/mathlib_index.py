from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from difflib import SequenceMatcher
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator

from lean_loop.jsonutil import utc_now
from lean_loop.process_control import ProcessCancelled, ProcessControl


INDEX_SCHEMA_VERSION = 1
SNIPPET_LIMIT = 300

_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*"
    r"(?P<kind>theorem|lemma|def|abbrev|structure|class|inductive|coinductive|"
    r"axiom|constant|opaque|instance)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_'.]*)"
)
_SYNTAX_NAME_RE = re.compile(r"\(\s*name\s*:=\s*([A-Za-z_][A-Za-z0-9_'.]*)\s*\)")
_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.]*)\s*$")
_END_RE = re.compile(r"^\s*end(?:\s+[A-Za-z_][A-Za-z0-9_'.]*)?\s*$")
_IMPORT_RE = re.compile(r"^\s*import\s+(.+?)\s*$")


@dataclass(frozen=True)
class IndexedHit:
    query: str
    name: str
    kind: str
    module: str
    path: str
    line: int
    snippet: str
    match: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IndexBuildResult:
    path: Path
    fingerprint: str
    files: int
    symbols: int
    imports: int
    duration_seconds: float
    size_bytes: int


def mathlib_package_root(project: Path) -> Path:
    root = project / ".lake" / "packages" / "mathlib"
    if not root.is_dir():
        raise FileNotFoundError(f"Mathlib package directory not found: {root}")
    return root


def mathlib_source_root(project: Path) -> Path:
    root = mathlib_package_root(project) / "Mathlib"
    if not root.is_dir():
        raise FileNotFoundError(f"Mathlib source directory not found: {root}")
    return root


def index_path(project: Path) -> Path:
    return project / ".lean-agent" / "indexes" / "mathlib.sqlite3"


def _git_dir(package: Path) -> Path | None:
    marker = package / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            candidate = Path(text.split(":", 1)[1].strip())
            return candidate if candidate.is_absolute() else (package / candidate).resolve()
    return None


def _git_commit(package: Path) -> str:
    git_dir = _git_dir(package)
    if git_dir is None:
        return ""
    head = git_dir / "HEAD"
    if not head.is_file():
        return ""
    value = head.read_text(encoding="ascii", errors="replace").strip()
    if value.startswith("ref:"):
        ref = git_dir / value.split(":", 1)[1].strip()
        if ref.is_file():
            return ref.read_text(encoding="ascii", errors="replace").strip()
        packed = git_dir / "packed-refs"
        if packed.is_file():
            ref_name = value.split(":", 1)[1].strip()
            for line in packed.read_text(encoding="ascii", errors="replace").splitlines():
                if line and not line.startswith("#") and line.endswith(" " + ref_name):
                    return line.split(" ", 1)[0]
        return value
    return value


def mathlib_fingerprint(project: Path) -> str:
    package = mathlib_package_root(project)
    entry = package / "Mathlib.lean"
    toolchain = project / "lean-toolchain"
    payload = {
        "schema": INDEX_SCHEMA_VERSION,
        "toolchain": toolchain.read_text(encoding="utf-8").strip() if toolchain.is_file() else "",
        "commit": _git_commit(package),
        "entry_size": entry.stat().st_size if entry.is_file() else 0,
        "entry_mtime_ns": entry.stat().st_mtime_ns if entry.is_file() else 0,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


@contextmanager
def _connection(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    if readonly:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    else:
        connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: row["value"]
        for row in connection.execute("SELECT key, value FROM metadata")
    }


def index_status(project: Path) -> dict[str, object]:
    path = index_path(project)
    if not path.is_file():
        return {"exists": False, "valid": False, "path": str(path)}
    try:
        with _connection(path, readonly=True) as connection:
            metadata = _metadata(connection)
            files = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            symbols = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            imports = connection.execute("SELECT COUNT(*) FROM module_imports").fetchone()[0]
    except (OSError, sqlite3.Error, KeyError) as exc:
        return {
            "exists": True,
            "valid": False,
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "error": str(exc),
        }
    current = mathlib_fingerprint(project)
    return {
        "exists": True,
        "valid": metadata.get("fingerprint") == current
        and metadata.get("schema_version") == str(INDEX_SCHEMA_VERSION),
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "fingerprint": metadata.get("fingerprint"),
        "current_fingerprint": current,
        "built_at": metadata.get("built_at"),
        "duration_seconds": float(metadata.get("duration_seconds", "0")),
        "files": files,
        "symbols": symbols,
        "imports": imports,
    }


def _qualified(namespace: list[str], name: str) -> str:
    if not namespace or name.startswith("_root_."):
        return name.removeprefix("_root_.")
    return ".".join([*namespace, name])


def build_mathlib_index(
    project: Path,
    *,
    force: bool = False,
    process_control: ProcessControl | None = None,
    progress: Callable[[int, int, Path], None] | None = None,
) -> IndexBuildResult:
    status = index_status(project)
    if status.get("valid") and not force:
        return IndexBuildResult(
            path=Path(str(status["path"])),
            fingerprint=str(status["fingerprint"]),
            files=int(status["files"]),
            symbols=int(status["symbols"]),
            imports=int(status["imports"]),
            duration_seconds=float(status["duration_seconds"]),
            size_bytes=int(status["size_bytes"]),
        )

    source_root = mathlib_source_root(project)
    files = sorted(source_root.rglob("*.lean"))
    destination = index_path(project)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.build-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    fingerprint = mathlib_fingerprint(project)
    started = time.perf_counter()
    symbol_count = 0
    import_count = 0
    try:
        with _connection(temporary) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = OFF;
                PRAGMA synchronous = OFF;
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE files (
                    path TEXT PRIMARY KEY,
                    module TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL
                );
                CREATE TABLE symbols (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    short_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    module TEXT NOT NULL,
                    path TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    snippet TEXT NOT NULL
                );
                CREATE TABLE module_imports (
                    module TEXT NOT NULL,
                    imported_module TEXT NOT NULL,
                    PRIMARY KEY (module, imported_module)
                );
                """
            )
            for file_number, path in enumerate(files, 1):
                if process_control is not None and process_control.cancel_requested():
                    raise ProcessCancelled("Cancelled while building the Mathlib index")
                relative = path.relative_to(source_root)
                module = "Mathlib." + ".".join(relative.with_suffix("").parts)
                stat = path.stat()
                connection.execute(
                    "INSERT INTO files(path, module, size_bytes, mtime_ns) VALUES (?, ?, ?, ?)",
                    (str(path), module, stat.st_size, stat.st_mtime_ns),
                )
                namespace: list[str] = []
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for line_number, line in enumerate(lines, 1):
                    namespace_match = _NAMESPACE_RE.match(line)
                    if namespace_match:
                        namespace.append(namespace_match.group(1))
                        continue
                    if _END_RE.match(line) and namespace:
                        namespace.pop()
                        continue
                    import_match = _IMPORT_RE.match(line)
                    if import_match:
                        for imported in import_match.group(1).split():
                            if imported.startswith("Mathlib"):
                                connection.execute(
                                    "INSERT OR IGNORE INTO module_imports(module, imported_module) VALUES (?, ?)",
                                    (module, imported),
                                )
                                import_count += 1
                    declaration = _DECL_RE.match(line)
                    if declaration:
                        short_name = declaration.group("name").split(".")[-1]
                        name = _qualified(namespace, declaration.group("name"))
                        connection.execute(
                            """INSERT INTO symbols(
                                name, short_name, kind, module, path, line, snippet
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                name,
                                short_name,
                                declaration.group("kind"),
                                module,
                                str(path),
                                line_number,
                                line.strip()[:SNIPPET_LIMIT],
                            ),
                        )
                        symbol_count += 1
                    for syntax_name in _SYNTAX_NAME_RE.findall(line):
                        connection.execute(
                            """INSERT INTO symbols(
                                name, short_name, kind, module, path, line, snippet
                            ) VALUES (?, ?, 'syntax', ?, ?, ?, ?)""",
                            (
                                _qualified(namespace, syntax_name),
                                syntax_name.split(".")[-1],
                                module,
                                str(path),
                                line_number,
                                line.strip()[:SNIPPET_LIMIT],
                            ),
                        )
                        symbol_count += 1
                if progress is not None:
                    progress(file_number, len(files), path)
            connection.executescript(
                """
                CREATE INDEX symbols_name_index ON symbols(name COLLATE NOCASE);
                CREATE INDEX symbols_short_name_index ON symbols(short_name COLLATE NOCASE);
                CREATE INDEX symbols_module_index ON symbols(module);
                """
            )
            duration = time.perf_counter() - started
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("schema_version", str(INDEX_SCHEMA_VERSION)),
                    ("fingerprint", fingerprint),
                    ("built_at", utc_now()),
                    ("duration_seconds", f"{duration:.6f}"),
                ],
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return IndexBuildResult(
        destination,
        fingerprint,
        len(files),
        symbol_count,
        import_count,
        time.perf_counter() - started,
        destination.stat().st_size,
    )


def search_index(project: Path, query: str, limit: int = 10) -> list[IndexedHit]:
    status = index_status(project)
    if not status.get("valid"):
        return []
    clean = query.strip()
    if not clean:
        return []
    short = clean.rsplit(".", 1)[-1]
    path = index_path(project)
    with _connection(path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT *,
                CASE
                  WHEN name = ? COLLATE NOCASE THEN 0
                  WHEN short_name = ? COLLATE NOCASE THEN 1
                  WHEN name LIKE ? ESCAPE '\\' THEN 2
                  ELSE 3
                END AS rank
               FROM symbols
               WHERE name = ? COLLATE NOCASE
                  OR short_name = ? COLLATE NOCASE
                  OR name LIKE ? ESCAPE '\\'
                  OR snippet LIKE ? ESCAPE '\\'
               ORDER BY rank, length(name), module
               LIMIT ?""",
            (
                clean,
                short,
                f"%.{short}",
                clean,
                short,
                f"%.{short}",
                f"%{clean}%",
                limit,
            ),
        ).fetchall()
    hits: list[IndexedHit] = []
    for row in rows:
        rank = int(row["rank"])
        # A qualified query such as ``Real.cos`` must not treat an unrelated
        # namespace's short-name match as an exact declaration. Unqualified
        # diagnostics (``pi_gt_three``) intentionally retain short-name exact
        # matching because that is how Lean reports many missing constants.
        if rank == 0:
            match = "exact"
        elif rank == 1 and "." not in clean:
            match = "exact"
        elif rank <= 2:
            match = "suffix"
        else:
            match = "snippet"
        hits.append(
            IndexedHit(
                query=clean,
                name=row["name"],
                kind=row["kind"],
                module=row["module"],
                path=row["path"],
                line=int(row["line"]),
                snippet=row["snippet"],
                match=match,
            )
        )
    return hits


def indexed_module_exists(project: Path, module: str) -> bool:
    status = index_status(project)
    if not status.get("valid"):
        return False
    clean = module.strip()
    if not clean:
        return False
    with _connection(index_path(project), readonly=True) as connection:
        row = connection.execute(
            "SELECT 1 FROM files WHERE module = ? LIMIT 1", (clean,)
        ).fetchone()
    return row is not None


def similar_indexed_modules(
    project: Path, module: str, limit: int = 5
) -> list[dict[str, object]]:
    status = index_status(project)
    if not status.get("valid") or limit < 1:
        return []
    clean = module.strip()
    if not clean:
        return []
    tail = clean.rsplit(".", 1)[-1]
    with _connection(index_path(project), readonly=True) as connection:
        rows = connection.execute(
            """SELECT module, path FROM files
               WHERE module LIKE ? COLLATE NOCASE
                  OR module LIKE ? COLLATE NOCASE""",
            (f"%{tail}%", f"%{tail[: max(4, len(tail) // 2)]}%"),
        ).fetchall()
        if len(rows) < limit:
            rows = connection.execute("SELECT module, path FROM files").fetchall()
    ranked: list[tuple[float, int, str, str]] = []
    clean_lower = clean.lower()
    clean_parts = set(clean_lower.split("."))
    for row in rows:
        candidate = str(row["module"])
        candidate_lower = candidate.lower()
        ratio = SequenceMatcher(None, clean_lower, candidate_lower).ratio()
        overlap = len(clean_parts.intersection(candidate_lower.split(".")))
        if tail.lower() == candidate_lower.rsplit(".", 1)[-1]:
            ratio += 0.25
        ranked.append((ratio, overlap, candidate, str(row["path"])))
    ranked.sort(key=lambda item: (-item[0], -item[1], len(item[2]), item[2]))
    return [
        {"module": candidate, "path": path, "score": round(score, 4)}
        for score, _overlap, candidate, path in ranked[:limit]
    ]
