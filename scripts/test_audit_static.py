#!/usr/bin/env python3
"""Static screen of the test suite for useless, duplicate and weak tests.

Stdlib only.  Walks ``tests/**/*.py`` with :mod:`ast` and flags, per test
function:

* ``no_assert``      no assertion of any kind (assert, ``pytest.raises``,
                     mock ``assert_*``, whitelisted assert helper, ...)
* ``tautology``      ``assert True``, ``assert x == x``, ``assert 1 == 1``,
                     ``assert m.called`` on a mock the test called itself
* ``duplicate``      identical or near-identical bodies across tests
* ``mock_heavy``     mock/patch constructs greatly exceed assertions
* ``weak_only``      the only assertions are ``is not None`` / ``isinstance`` /
                     bare truthiness
* ``long``           very long bodies

Every flag is a *candidate* for review, never a verdict.  Output is Markdown
plus JSON under ``.local/test-audit/`` by default.

    uv run python scripts/test_audit_static.py [--root tests] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

LONG_LINES = 80
MOCK_MIN = 8  # at least this many mock constructs ...
MOCK_RATIO = 4.0  # ... and mocks / (asserts + 1) at least this
DUP_MIN_NODES = 30  # ignore tiny bodies when clustering
DUP_MIN_STMTS = 2

# Names whose call counts as an assertion (matched on the last dotted part).
HELPER_NAME_RE = re.compile(r"^_*(assert|check|verify|expect|ensure|validate)", re.I)
# Fixtures / callables that assert internally (snapshot comparison etc.).
ASSERTING_CALLABLES = frozenset(
    {
        "snap_compare",
        "assert_snapshot",
        "snapshot",
        "match_snapshot",
        "assert_match",
        "fail",  # pytest.fail
        "xfail",
        "check_returncode",
        "assertRaises",
    }
)
MOCK_ASSERT_PREFIXES = (
    "assert_called",
    "assert_any_call",
    "assert_has_calls",
    "assert_not_called",
    "assert_awaited",
)
SNAPSHOT_CALLS = frozenset({"snap_compare", "snapshot", "match_snapshot"})
RAISES_CTX = frozenset({"raises", "warns", "deprecated_call", "fails"})
MOCK_FACTORIES = frozenset(
    {"MagicMock", "Mock", "AsyncMock", "NonCallableMock", "PropertyMock", "create_autospec"}
)
MOCK_CALL_ATTRS = frozenset(
    {
        "patch",
        "object",
        "dict",
        "multiple",
        "setattr",
        "setitem",
        "delattr",
        "delitem",
        "setenv",
        "delenv",
        "spy",
        "stub",
        "Mock",
        "MagicMock",
        "AsyncMock",
    }
)


@dataclass
class TestInfo:
    __test__ = False  # not a pytest class

    file: str
    name: str  # Class.method or function
    line: int
    end_line: int
    lines: int = 0
    asserts: int = 0
    raises: int = 0
    mock_asserts: int = 0
    helper_calls: list[str] = field(default_factory=list)
    mocks: int = 0
    stmts: int = 0
    nodes: int = 0
    parametrized: bool = False
    flags: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.file}::{self.name}"


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------


def _last_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _is_test_func(node: ast.AST) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def iter_tests(
    tree: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    out: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in tree.body:
        if _is_test_func(node):
            assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            out.append((node.name, node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if _is_test_func(sub):
                    assert isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef)
                    out.append((f"{node.name}.{sub.name}", sub))
    return out


def _direct_asserting(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Does this function body assert by itself (no helper resolution)?"""
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Raise) and n.exc is not None:
            if _last_name(n.exc.func if isinstance(n.exc, ast.Call) else n.exc) == "AssertionError":
                return True
        if isinstance(n, ast.Call):
            nm = _last_name(n.func)
            if nm in RAISES_CTX or nm in ASSERTING_CALLABLES or nm.startswith("assert_"):
                return True
    return False


def discover_asserting_helpers(trees: dict[str, ast.Module]) -> dict[str, str]:
    """Names of functions in the test tree that (transitively) assert.

    Returns name -> "direct" | "transitive".  Test functions themselves are
    excluded (they are what we audit).  Name collisions across files are
    merged: a name counts as asserting if *any* definition asserts.
    """
    defs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = defaultdict(list)
    for tree in trees.values():
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith(
                "test"
            ):
                defs[n.name].append(n)
    result: dict[str, str] = {}
    for name, fns in defs.items():
        if any(_direct_asserting(f) for f in fns):
            result[name] = "direct"
    changed = True
    while changed:
        changed = False
        for name, fns in defs.items():
            if name in result:
                continue
            for f in fns:
                called = {_last_name(c.func) for c in ast.walk(f) if isinstance(c, ast.Call)}
                if called & result.keys():
                    result[name] = "transitive"
                    changed = True
                    break
    return result


# --------------------------------------------------------------------------
# Per-test analysis
# --------------------------------------------------------------------------


def _is_weak(test: ast.expr) -> bool:
    if isinstance(test, ast.Constant):
        return False  # handled as tautology
    if isinstance(test, ast.Name | ast.Attribute | ast.Subscript):
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _is_weak(test.operand)
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        right = test.comparators[0]
        if (
            isinstance(test.ops[0], ast.IsNot)
            and isinstance(right, ast.Constant)
            and right.value is None
        ):
            return True
    if isinstance(test, ast.Call) and _last_name(test.func) in {"isinstance", "callable"}:
        return True
    return False


def _tautology(test: ast.expr, mock_names: set[str], called_direct: set[str]) -> str | None:
    if isinstance(test, ast.Constant) and test.value:
        return f"assert {test.value!r}"
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        if isinstance(test.operand, ast.Constant) and not test.operand.value:
            return "assert not <falsy const>"
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, right = test.left, test.comparators[0]
        op = test.ops[0]
        if isinstance(op, ast.Eq | ast.Is | ast.LtE | ast.GtE) and ast.dump(left) == ast.dump(
            right
        ):
            has_call = any(isinstance(n, ast.Call) for n in ast.walk(left))
            if not has_call:
                return f"assert {ast.unparse(left)} == itself"
        if (
            isinstance(left, ast.Constant)
            and isinstance(right, ast.Constant)
            and isinstance(op, ast.Eq)
            and left.value == right.value
        ):
            return "assert const == same const"
    # assert m.called / m.call_count on a mock the test itself called directly
    if isinstance(test, ast.Attribute) and test.attr == "called":
        base = test.value
        if isinstance(base, ast.Name) and base.id in mock_names and base.id in called_direct:
            return f"assert {base.id}.called after calling {base.id}() directly"
    return None


def analyse_test(
    file: str,
    name: str,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    helpers: dict[str, str],
    helper_hits: Counter[str],
) -> TestInfo:
    end = fn.end_lineno or fn.lineno
    info = TestInfo(file=file, name=name, line=fn.lineno, end_line=end, lines=end - fn.lineno + 1)
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    info.stmts = len(body)
    info.nodes = sum(1 for s in body for _ in ast.walk(s))
    info.parametrized = any(
        "parametrize" in _dotted(d.func if isinstance(d, ast.Call) else d)
        for d in fn.decorator_list
    )

    # mock decorators count as mock usage
    for d in fn.decorator_list:
        dn = _dotted(d.func if isinstance(d, ast.Call) else d)
        if dn.endswith("patch") or ".patch." in dn + ".":
            info.mocks += 1

    mock_names: set[str] = set()
    called_direct: set[str] = set()
    for stmt in body:
        for n in ast.walk(stmt):
            if (
                isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Call)
            ):
                f = n.value.func
                if _last_name(f) in MOCK_FACTORIES:
                    mock_names.add(n.targets[0].id)
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
        ):
            called_direct.add(stmt.value.func.id)

    arg_names = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    asserting: list[ast.Assert] = []
    taut: list[str] = []

    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Assert):
                info.asserts += 1
                asserting.append(n)
                t = _tautology(n.test, mock_names, called_direct)
                if t:
                    taut.append(f"L{n.lineno}: {t}")
            elif isinstance(n, ast.Raise) and n.exc is not None:
                tgt = n.exc.func if isinstance(n.exc, ast.Call) else n.exc
                if _last_name(tgt) == "AssertionError":
                    info.asserts += 1
            elif isinstance(n, ast.Call):
                nm = _last_name(n.func)
                dotted = _dotted(n.func)
                if nm in RAISES_CTX:
                    info.raises += 1
                elif nm in ("fail", "xfail") and dotted.startswith("pytest"):
                    info.raises += 1
                elif nm.startswith("assert_") or nm in {
                    "assert_called",
                    "assert_not_called",
                    "assert_awaited",
                }:
                    if isinstance(n.func, ast.Attribute) and nm.startswith(MOCK_ASSERT_PREFIXES):
                        info.mock_asserts += 1
                    else:
                        info.helper_calls.append(nm)
                        helper_hits[nm] += 1
                elif nm in SNAPSHOT_CALLS:
                    info.helper_calls.append(nm)
                    helper_hits[nm] += 1
                elif nm in helpers or HELPER_NAME_RE.match(nm):
                    # skip plain stdlib / mock names that merely look like helpers
                    if not isinstance(n.func, ast.Attribute) or nm in helpers:
                        info.helper_calls.append(nm)
                        helper_hits[nm] += 1
                # mock usage constructs
                if nm in MOCK_FACTORIES:
                    info.mocks += 1
                elif nm in MOCK_CALL_ATTRS and isinstance(n.func, ast.Attribute):
                    base = _dotted(n.func.value)
                    if (
                        base.endswith(("patch", "mocker", "monkeypatch", "mock"))
                        or base in {"patch", "mocker", "monkeypatch", "mock"}
                        or "patch" in base
                        or "monkeypatch" in base
                        or "mocker" in base
                    ):
                        info.mocks += 1
                elif nm == "patch" and isinstance(n.func, ast.Name):
                    info.mocks += 1
                # sub-checks: self.assertXxx (unittest style)
                if isinstance(n.func, ast.Attribute) and re.match(r"^assert[A-Z]", nm):
                    info.helper_calls.append(nm)
    # snapshot fixtures used purely as an argument
    for a in arg_names & {"snap_compare", "snapshot"}:
        used = any(isinstance(n, ast.Name) and n.id == a for s in body for n in ast.walk(s))
        if used and a not in info.helper_calls:
            info.helper_calls.append(a)
            helper_hits[a] += 1

    total_checks = info.asserts + info.raises + info.mock_asserts + len(info.helper_calls)

    # (a) no assertion at all
    if total_checks == 0:
        info.flags.append("no_assert")
    # (b) tautology
    if taut:
        info.flags.append("tautology")
        info.detail["tautology"] = taut
    # (e) only weak assertions
    if (
        info.asserts > 0
        and info.raises == 0
        and info.mock_asserts == 0
        and not info.helper_calls
        and all(_is_weak(a.test) for a in asserting)
    ):
        info.flags.append("weak_only")
        info.detail["weak"] = [f"L{a.lineno}: {ast.unparse(a.test)[:70]}" for a in asserting][:5]
    # (d) mock heavy
    if info.mocks >= MOCK_MIN and info.mocks / (total_checks + 1) >= MOCK_RATIO:
        info.flags.append("mock_heavy")
    # (f) long
    if info.lines >= LONG_LINES:
        info.flags.append("long")
    return info


# --------------------------------------------------------------------------
# Duplicate clustering
# --------------------------------------------------------------------------


class _Canon(ast.NodeTransformer):
    def __init__(self, rename: bool, literals: bool) -> None:
        self.rename = rename
        self.literals = literals
        self.map: dict[str, str] = {}
        self.locals: set[str] = set()

    def prime(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
            self.locals.add(a.arg)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store | ast.Del):
                self.locals.add(n.id)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                self.locals.add(n.name)
            elif isinstance(n, ast.arg):
                self.locals.add(n.arg)

    def _nm(self, ident: str) -> str:
        if ident not in self.map:
            self.map[ident] = f"v{len(self.map)}"
        return self.map[ident]

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if self.rename and node.id in self.locals:
            node.id = self._nm(node.id)
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        if self.rename and node.arg in self.locals:
            node.arg = self._nm(node.arg)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if self.literals:
            return ast.copy_location(ast.Constant(value=type(node.value).__name__), node)
        return node


def body_keys(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, str, str]:
    stmts = list(fn.body)
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
        stmts = stmts[1:]

    def dump(rename: bool, literals: bool) -> str:
        c = _Canon(rename, literals)
        probe = ast.Module(body=copy.deepcopy(stmts), type_ignores=[])
        wrapper = ast.FunctionDef(
            name="f",
            args=copy.deepcopy(fn.args),
            body=probe.body or [ast.Pass()],
            decorator_list=[],
            returns=None,
            type_params=[],
        )
        if rename:
            c.prime(wrapper)
        node = c.visit(wrapper)
        return ast.dump(node, annotate_fields=False, include_attributes=False)

    def h(text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()

    return h(dump(False, False)), h(dump(True, False)), h(dump(True, True))


def cluster(infos: list[TestInfo], keys: dict[str, tuple[str, str, str]]) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    taken: set[str] = set()
    cand = [i for i in infos if i.nodes >= DUP_MIN_NODES and i.stmts >= DUP_MIN_STMTS]
    for tier, idx in (("exact", 0), ("renamed", 1), ("shape", 2)):
        groups: dict[str, list[TestInfo]] = defaultdict(list)
        for i in cand:
            if i.id in taken:
                continue
            groups[keys[i.id][idx]].append(i)
        for members in groups.values():
            if len(members) < 2:
                continue
            files = {m.file for m in members}
            clusters.append(
                {
                    "tier": tier,
                    "size": len(members),
                    "nodes": members[0].nodes,
                    "same_file": len(files) == 1,
                    "parametrize_candidate": (
                        tier in ("renamed", "shape")
                        and len(files) == 1
                        and not any(m.parametrized for m in members)
                    ),
                    "members": [f"{m.id} (L{m.line})" for m in members],
                }
            )
            taken.update(m.id for m in members)
    clusters.sort(key=lambda c: (-c["size"] * c["nodes"], c["tier"]))
    return clusters


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def scan(root: Path, rel_to: Path) -> dict[str, Any]:
    trees: dict[str, ast.Module] = {}
    errors: list[str] = []
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = str(p.relative_to(rel_to))
        try:
            trees[rel] = ast.parse(p.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"{rel}: {exc}")

    helpers = discover_asserting_helpers(trees)
    helper_hits: Counter[str] = Counter()
    infos: list[TestInfo] = []
    keys: dict[str, tuple[str, str, str]] = {}
    for rel, tree in trees.items():
        for name, fn in iter_tests(tree):
            info = analyse_test(rel, name, fn, helpers, helper_hits)
            infos.append(info)
            keys[info.id] = body_keys(fn)

    clusters = cluster(infos, keys)
    in_cluster = {m.split(" (L")[0] for c in clusters for m in c["members"]}
    for i in infos:
        if i.id in in_cluster:
            i.flags.append("duplicate")

    by_cat: Counter[str] = Counter()
    for i in infos:
        for f in i.flags:
            by_cat[f] += 1

    return {
        "tests": infos,
        "clusters": clusters,
        "helpers": helpers,
        "helper_hits": helper_hits,
        "by_cat": by_cat,
        "files": len(trees),
        "errors": errors,
    }


def _top(infos: list[TestInfo], flag: str, key: Any, n: int = 25) -> list[TestInfo]:
    sel = [i for i in infos if flag in i.flags]
    sel.sort(key=key, reverse=True)
    return sel[:n]


def render_md(res: dict[str, Any]) -> str:
    infos: list[TestInfo] = res["tests"]
    cl: list[dict[str, Any]] = res["clusters"]
    out: list[str] = ["# Static test audit", ""]
    out += [
        f"Scanned {res['files']} files, {len(infos)} test functions "
        f"({sum(1 for i in infos if i.parametrized)} parametrized).",
        "",
        "All flags are review candidates, not verdicts.",
        "",
        "## Counts per category",
        "",
        "| category | tests |",
        "|---|---|",
    ]
    for cat in ("no_assert", "tautology", "duplicate", "mock_heavy", "weak_only", "long"):
        out.append(f"| {cat} | {res['by_cat'].get(cat, 0)} |")
    out.append("")
    out.append(
        f"Duplicate clusters: {len(cl)} "
        f"(exact {sum(c['tier'] == 'exact' for c in cl)}, "
        f"renamed {sum(c['tier'] == 'renamed' for c in cl)}, "
        f"shape {sum(c['tier'] == 'shape' for c in cl)}; "
        f"parametrize candidates {sum(c['parametrize_candidate'] for c in cl)})."
    )
    out.append("")

    def table(title: str, sel: list[TestInfo], extra: Any = None) -> None:
        out.append(f"## {title}")
        out.append("")
        if not sel:
            out.extend(["(none)", ""])
            return
        for i in sel:
            e = f" - {extra(i)}" if extra else ""
            out.append(f"- `{i.id}` (L{i.line}, {i.lines} lines){e}")
        out.append("")

    table(
        "No assertion (top 40 by body size)",
        _top(infos, "no_assert", lambda i: i.nodes, 40),
        lambda i: f"{i.stmts} stmts, mocks={i.mocks}",
    )
    table(
        "Tautologies (all)",
        _top(infos, "tautology", lambda i: i.line, 1000),
        lambda i: "; ".join(i.detail["tautology"]),
    )
    out.append("## Duplicate clusters (top 40 by size x body)")
    out.append("")
    for c in cl[:40]:
        tag = (
            "parametrize candidate"
            if c["parametrize_candidate"]
            else ("same file" if c["same_file"] else "cross-file")
        )
        out.append(f"- **{c['tier']}** x{c['size']} ({c['nodes']} nodes, {tag})")
        for m in c["members"][:6]:
            out.append(f"  - `{m}`")
        if c["size"] > 6:
            out.append(f"  - ... and {c['size'] - 6} more")
    out.append("")
    table(
        "Mock-heavy (top 25 by mock/assert ratio)",
        _top(
            infos,
            "mock_heavy",
            lambda i: i.mocks / (i.asserts + i.raises + i.mock_asserts + len(i.helper_calls) + 1),
        ),
        lambda i: f"mocks={i.mocks} asserts={i.asserts} mock_asserts={i.mock_asserts}",
    )
    table(
        "Weak-only assertions (top 40 by file order)",
        _top(infos, "weak_only", lambda i: -i.line, 40),
        lambda i: "; ".join(i.detail["weak"][:2]),
    )
    table("Longest tests (top 25)", _top(infos, "long", lambda i: i.lines))

    out.append("## Whitelisted assert helpers")
    out.append("")
    out.append(
        "Discovered by scanning test-tree function definitions that assert directly "
        "or call one that does (transitive), plus name patterns "
        "`assert*/check*/verify*/expect*/ensure*/validate*`, `snap_compare`/`snapshot` "
        "fixtures, `pytest.raises/warns/fail`, mock `assert_*` methods and `self.assertX`."
    )
    out.append("")
    hits: Counter[str] = res["helper_hits"]
    helpers: dict[str, str] = res["helpers"]
    out.append("| helper | how found | tests using it |")
    out.append("|---|---|---|")
    for nm, cnt in hits.most_common(80):
        how = helpers.get(nm, "name pattern only (unverified)")
        out.append(f"| `{nm}` | {how} | {cnt} |")
    out.append("")
    out.append(
        f"Discovered asserting helper functions in total: {len(helpers)} "
        f"(only those actually called from a test are listed above)."
    )
    if res["errors"]:
        out += ["", "## Parse errors", ""] + [f"- {e}" for e in res["errors"]]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    ap.add_argument("--root", default="tests", type=Path)
    ap.add_argument("--out-dir", default=Path(".local/test-audit"), type=Path)
    args = ap.parse_args(argv)

    res = scan(args.root.resolve(), Path.cwd())
    args.out_dir.mkdir(parents=True, exist_ok=True)
    md = render_md(res)
    (args.out_dir / "static-report.md").write_text(md, encoding="utf-8")
    payload = {
        "files": res["files"],
        "counts": dict(res["by_cat"]),
        "tests": [asdict(i) | {"id": i.id} for i in res["tests"] if i.flags],
        "clusters": res["clusters"],
        "helpers_whitelisted": dict(res["helper_hits"]),
        "helpers_discovered": res["helpers"],
        "errors": res["errors"],
    }
    (args.out_dir / "static-report.json").write_text(
        json.dumps(payload, indent=1), encoding="utf-8"
    )
    print(md.split("## No assertion")[0])
    print(f"wrote {args.out_dir}/static-report.md and .json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
