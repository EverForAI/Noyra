"""Keep domain adapters independent of the runtime composition root."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path


def _runtime_imports(node: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    imports: list[ast.Import | ast.ImportFrom] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If) and (
            isinstance(child.test, ast.Name) and child.test.id == "TYPE_CHECKING"
        ):
            for alternative in child.orelse:
                imports.extend(_runtime_imports(alternative))
            continue
        if isinstance(child, ast.Import | ast.ImportFrom):
            imports.append(child)
        imports.extend(_runtime_imports(child))
    return imports


def test_http_domains_do_not_import_the_runtime_composition_root() -> None:
    root = Path(__file__).resolve().parents[1] / "src/noyra/http_routes"
    paths = list(root.glob("*.py"))
    assert paths, "HTTP domains must be packaged"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _runtime_imports(tree):
            modules = (
                [resolve_name("." * node.level + (node.module or ""), "noyra.http_routes")]
                if isinstance(node, ast.ImportFrom)
                else [name.name for name in node.names]
            )
            assert "noyra.service" not in modules, (path.name, node.lineno)
            if isinstance(node, ast.ImportFrom):
                assert not (
                    modules == ["noyra"] and any(alias.name == "service" for alias in node.names)
                ), (path.name, node.lineno)


def test_schema_definition_modules_cannot_depend_on_the_database_facade() -> None:
    root = Path(__file__).resolve().parents[1] / "src/noyra/core/schema"
    paths = list(root.glob("*.py"))
    assert paths, "Frozen schema definitions must be packaged"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _runtime_imports(tree):
            modules = (
                [resolve_name("." * node.level + (node.module or ""), "noyra.core.schema")]
                if isinstance(node, ast.ImportFrom)
                else [name.name for name in node.names]
            )
            assert "noyra.core.database" not in modules, (path.name, node.lineno)
            if isinstance(node, ast.ImportFrom):
                assert not (
                    modules == ["noyra.core"]
                    and any(alias.name == "database" for alias in node.names)
                ), (path.name, node.lineno)
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                assert not (
                    isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "sqlite3"
                    and call.func.attr == "connect"
                ), (path.name, call.lineno)


def test_configuration_and_transport_do_not_import_the_runtime_root() -> None:
    root = Path(__file__).resolve().parents[1] / "src/noyra"
    for name in ("service_configuration.py", "http_transport.py"):
        path = root / name
        for node in _runtime_imports(ast.parse(path.read_text(encoding="utf-8"))):
            modules = (
                [resolve_name("." * node.level + (node.module or ""), "noyra")]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
            )
            assert "noyra.service" not in modules, (name, node.lineno)
            if isinstance(node, ast.ImportFrom):
                assert not (
                    modules == ["noyra"] and any(alias.name == "service" for alias in node.names)
                ), (name, node.lineno)
