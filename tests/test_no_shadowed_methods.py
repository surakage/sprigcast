"""A class that defines a method twice silently loses the first one.

MainWindow had two _camera_settings: the Starter kit's (a camera device -> settings) replaced the Live
gear's (open the Virtual camera pop-up), so the gear raised an error on every click and did nothing.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sprigcast"


def test_no_class_defines_a_method_twice():
    twice = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            seen: dict[str, int] = {}
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorators = [ast.unparse(d) for d in item.decorator_list]
                if any(d.endswith((".setter", ".deleter")) or d in ("overload", "typing.overload") for d in decorators):
                    continue
                if item.name in seen:
                    twice.append(f"{path.relative_to(SRC)}: {node.name}.{item.name} (lines {seen[item.name]}, {item.lineno})")
                seen[item.name] = item.lineno
    assert not twice, twice
