"""Jede Bibliothek, die der Anwendungscode importiert, muss als Laufzeit-Abhängigkeit deklariert sein.

Hintergrund: httpx war nur Test-Abhängigkeit; lokal und in CI lief alles, im Docker-Image
(ohne [dev]) stürzte der Processing-Worker beim Start ab.
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, packages_distributions, requires
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
# Importname ≠ Paketname, falls das Paket lokal nicht installiert ist (OCR-Extras)
ALIASES = {"PIL": "pillow", "fitz": "pymupdf"}


def _norm(name: str) -> str:
    return re.split(r"[<>=!~\[;\s(]", name, maxsplit=1)[0].lower().replace("_", "-")


def _with_transitive(declared: set[str]) -> set[str]:
    """Direkt deklarierte Pakete plus deren Laufzeit-Abhängigkeiten (z. B. starlette über fastapi)."""
    out = set(declared)
    for dist in declared:
        try:
            reqs = requires(dist) or []
        except PackageNotFoundError:
            continue
        out.update(_norm(r) for r in reqs if "extra ==" not in r)
    return out


def _declared() -> set[str]:
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    specs = list(project["dependencies"]) + list(project.get("optional-dependencies", {}).get("ocr", []))
    return {_norm(spec) for spec in specs}


def _imported_top_level() -> set[str]:
    names: set[str] = set()
    for path in (BACKEND / "invoiceflow").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {n for n in names if n != "invoiceflow" and n not in sys.stdlib_module_names}


def test_all_imported_packages_are_runtime_dependencies():
    declared = _with_transitive(_declared())
    mapping = packages_distributions()
    missing = []
    for module in sorted(_imported_top_level()):
        dists = {_norm(d) for d in mapping.get(module, [ALIASES.get(module, module)])}
        if not dists & declared:
            missing.append(f"{module} ({', '.join(sorted(dists))})")
    assert not missing, "Im Code importiert, aber nicht in [project.dependencies]: " + "; ".join(missing)
