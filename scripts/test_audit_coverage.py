#!/usr/bin/env python3
"""Per-test coverage redundancy report from coverage.py dynamic contexts.

Input: one or more coverage data files recorded with
``pytest --cov=src/jailbee --cov-context=test`` (line coverage only), plus
optional JUnit XML files (``--junitxml``) for durations and for tests that
cover nothing, and the static audit JSON (``static-report.json``) for the
"keeper" suggestion.  Needs the ``coverage`` package (run it through
``uv run --with coverage``).

Reports:

* tests that cover no function-body line of ``src/jailbee``
* groups of tests with *identical* covered line sets, and tests whose set is a
  strict subset of another single test's set (redundant-coverage candidates)
* per-test unique-line counts (tests with zero unique lines)
* the slowest tests, when JUnit XML is given

Only lines inside function/method bodies count: module- and class-level
statements run at import time and would be credited to whichever test happened
to import the module first.  Pass ``--include-import-time`` to count them.

CAVEAT: a subset by lines means *redundant coverage*, not redundant
assertions.  The output is a candidate list for human or LLM review, never an
auto-delete list.

Recording the data (about 100 s per shard with ``-n 8``; five shards, each run
in the FOREGROUND because one Bash call is limited to 10 minutes; needs PyPI
egress for ``uv run --with``).  Split the test files into five groups of
similar line count, then for each group N in 1..5::

    mkdir -p .local/test-audit/cov .local/test-audit/shards
    uv run python - <<'PY'
    import pathlib
    def size(p): return len(p.read_text().splitlines())
    files = sorted(pathlib.Path("tests").glob("test_*.py"), key=lambda p: -size(p))
    groups = [[] for _ in range(5)]
    sizes = [0] * 5
    for f in files:
        i = sizes.index(min(sizes))
        groups[i].append(str(f))
        sizes[i] += size(f)
    for n, g in enumerate(groups, 1):
        pathlib.Path(f".local/test-audit/shards/{n}.txt").write_text("\\n".join(g) + "\\n")
    PY

    export JAILBEE_NO_UPDATE_CHECK=1
    COVERAGE_FILE=.local/test-audit/cov/shardN uv run --with coverage --with pytest-cov \\
      pytest $(cat .local/test-audit/shards/N.txt) -n 8 -q --cov=src/jailbee \\
      --cov-context=test --cov-report= -p no:cacheprovider \\
      --junitxml=.local/test-audit/cov/shardN.xml --timeout=600

Then combine and report (the static report must exist first for the keeper
hints: ``uv run python scripts/test_audit_static.py``)::

    (cd .local/test-audit/cov && uv run --with coverage coverage combine --keep \\
      --data-file=combined shard1 shard2 shard3 shard4 shard5)
    uv run --with coverage python scripts/test_audit_coverage.py
    uv run --with coverage python scripts/test_audit_coverage.py --common-threshold 0 --tag=_raw
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

from coverage import CoverageData  # type: ignore[import-not-found,unused-ignore]  # optional, via uv --with

# Tests covering more distinctive lines than this are broad end-to-end style tests
# (Textual app boot, doctor, new_container); equal/subset coverage means little for
# them, so they are listed after the narrow ones.
BROAD = 400
PHASE_RE = re.compile(r"\|(setup|run|teardown)$")
PARAM_RE = re.compile(r"\[.*\]$")


def test_key(context: str, phases: frozenset[str]) -> str | None:
    """Test id of a context, or None when its phase is not counted."""
    m = PHASE_RE.search(context)
    if m and m.group(1) not in phases:
        return None
    return PHASE_RE.sub("", context)


def func_id(test_id: str) -> str:
    """Test id without the parametrize suffix."""
    return PARAM_RE.sub("", test_id)


def static_id(test_id: str) -> str:
    """Map a pytest node id to the static audit's ``file::Class.method`` id."""
    base = func_id(test_id)
    parts = base.split("::")
    return parts[0] + "::" + ".".join(parts[1:])


def body_lines(path: Path) -> set[int]:
    """Lines inside function bodies of a source file (excluding the def line)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            body = n.body if isinstance(n.body, list) else [n.body]
            first = body[0].lineno
            last = n.end_lineno or first
            out.update(range(first, last + 1))
    return out


def load_tests(
    data_files: list[Path], include_import_time: bool, phases: frozenset[str]
) -> tuple[dict[str, set[int]], list[str]]:
    """Return {test id: set of global line ids} and the id -> (file, line) table."""
    line_ids: dict[tuple[str, int], int] = {}
    table: list[str] = []
    tests: dict[str, set[int]] = defaultdict(set)
    body_cache: dict[str, set[int]] = {}
    for df in data_files:
        data = CoverageData(basename=str(df))
        data.read()
        for fname in data.measured_files():
            if "/jailbee/" not in fname.replace("\\", "/"):
                continue
            keep = (
                None
                if include_import_time
                else body_cache.setdefault(fname, body_lines(Path(fname)))
            )
            for lineno, ctxs in data.contexts_by_lineno(fname).items():
                if keep is not None and lineno not in keep:
                    continue
                key = (fname, lineno)
                gid = line_ids.get(key)
                if gid is None:
                    gid = len(table)
                    line_ids[key] = gid
                    short = fname.split("/src/", 1)[-1]
                    table.append(f"{short}:{lineno}")
                for c in ctxs:
                    key_t = test_key(c, phases) if c else None  # '' = import time
                    if key_t is not None:
                        tests[key_t].add(gid)
    return tests, table


def junit_info(paths: list[Path]) -> tuple[dict[str, float], set[str]]:
    times: dict[str, float] = {}
    for p in paths:
        for tc in ET.parse(p).getroot().iter("testcase"):
            cls = tc.get("classname", "")
            name = tc.get("name", "")
            segs = cls.split(".")
            # tests.test_x[.TestClass] -> tests/test_x.py[::TestClass]
            mod = "/".join(segs[:2]) + ".py"
            klass = [s for s in segs[2:]]
            nid = "::".join([mod, *klass, name])
            times[nid] = times.get(nid, 0.0) + float(tc.get("time", "0"))
    return times, set(times)


def drop_common_lines(
    tests: dict[str, set[int]], threshold: float
) -> tuple[dict[str, set[int]], int, list[str]]:
    """Remove lines hit by more than ``threshold`` of all tests (app boot, config load).

    Returns the reduced map (tests left with no line are dropped), the number
    of lines removed and the tests that were left with nothing.
    """
    if threshold <= 0:
        return tests, 0, []
    freq: dict[int, int] = defaultdict(int)
    for lines in tests.values():
        for g in lines:
            freq[g] += 1
    limit = threshold * len(tests)
    common = {g for g, n in freq.items() if n > limit}
    reduced = {t: lines - common for t, lines in tests.items()}
    empty = sorted(t for t, lines in reduced.items() if not lines)
    return {t: lines for t, lines in reduced.items() if lines}, len(common), empty


def analyse(tests: dict[str, set[int]], stats: dict[str, list[int]]) -> dict[str, Any]:
    ids = sorted(tests)
    masks: list[int] = []
    for t in ids:
        nbytes = (max(tests[t]) // 8) + 1
        buf = bytearray(nbytes)
        for g in tests[t]:
            buf[g >> 3] |= 1 << (g & 7)
        masks.append(int.from_bytes(buf, "little"))
    sizes = [len(tests[t]) for t in ids]

    index: dict[int, list[int]] = defaultdict(list)
    for i, t in enumerate(ids):
        for g in tests[t]:
            index[g].append(i)

    equal: dict[int, list[int]] = defaultdict(list)
    supersets: dict[int, list[int]] = {}
    for i, t in enumerate(ids):
        rare = min(tests[t], key=lambda g: len(index[g]))
        sup: list[int] = []
        for j in index[rare]:
            if j == i or sizes[j] < sizes[i]:
                continue
            if masks[i] & masks[j] == masks[i]:
                if sizes[j] == sizes[i]:
                    equal[i].append(j)
                else:
                    sup.append(j)
        if sup:
            supersets[i] = sup

    unique = {ids[i]: 0 for i in range(len(ids))}
    for owners in index.values():
        if len(owners) == 1:
            unique[ids[owners[0]]] += 1

    def checks(t: str) -> tuple[int, int]:
        s = stats.get(static_id(t))
        return (s[0], s[1]) if s else (0, 0)

    # equal-set clusters (connected via equality, which is an equivalence)
    seen: set[int] = set()
    clusters: list[list[str]] = []
    for i in equal:
        if i in seen:
            continue
        grp = {i, *equal[i]}
        seen |= grp
        clusters.append(sorted(ids[k] for k in grp))
    same_fn_clusters = [c for c in clusters if len({func_id(t) for t in c}) == 1]
    cross_clusters = [c for c in clusters if len({func_id(t) for t in c}) > 1]

    subset_rows: list[dict[str, Any]] = []
    for i, sup in supersets.items():
        cross = [j for j in sup if func_id(ids[j]) != func_id(ids[i])]
        if not cross:
            continue  # only param siblings are supersets
        cross.sort(key=lambda j: sizes[j])
        subset_rows.append(
            {
                "test": ids[i],
                "lines": sizes[i],
                "checks": checks(ids[i])[0],
                "n_supersets": len(cross),
                "closest_superset": ids[cross[0]],
                "closest_superset_lines": sizes[cross[0]],
                "closest_superset_checks": checks(ids[cross[0]])[0],
            }
        )
    subset_rows.sort(key=lambda r: (r["lines"] > BROAD, -r["lines"], r["test"]))

    def keeper(c: list[str]) -> str:
        return max(c, key=lambda t: (checks(t), t))

    return {
        "n_tests": len(ids),
        "n_lines": len({g for t in tests.values() for g in t}),
        "equal_clusters_cross_function": [
            {"keeper_suggestion": keeper(c), "lines": len(tests[c[0]]), "members": c}
            for c in sorted(cross_clusters, key=lambda c: (len(tests[c[0]]) > BROAD, -len(c), c[0]))
        ],
        "equal_clusters_param_siblings": len(same_fn_clusters),
        "strict_subset_tests": subset_rows,
        "unique": unique,
        "tests_zero_unique": sorted(t for t, n in unique.items() if n == 0),
    }


def render(
    res: dict[str, Any],
    note: str,
    no_cover: list[str],
    times: dict[str, float],
    n_xml_tests: int,
) -> str:
    out: list[str] = ["# Coverage redundancy audit", ""]
    out.append(
        "CAVEAT: a covered-line subset means redundant *coverage*, not redundant "
        "*assertions*. These lists are candidates for human or LLM review, never an "
        "auto-delete list. Lines counted: function/method bodies of `src/jailbee` only; "
        "only the pytest `run` phase is counted by default (see --phases)."
    )
    out.append("")
    out.append("## Headline numbers")
    out.append("")
    out.append(note)
    out.append("")
    out.append(f"- tests with recorded coverage contexts: {res['n_tests']}")
    out.append(f"- tests seen in JUnit XML: {n_xml_tests}")
    out.append(f"- distinct src lines (function bodies) covered by any test: {res['n_lines']}")
    out.append(f"- (a) tests covering no src function-body line: {len(no_cover)}")
    out.append(
        f"- (b) equal-coverage clusters across different test functions: "
        f"{len(res['equal_clusters_cross_function'])} "
        f"(plus {res['equal_clusters_param_siblings']} clusters of "
        "parametrize siblings, not listed)"
    )
    out.append(
        f"- (b) tests whose line set is a strict subset of another test function's: "
        f"{len(res['strict_subset_tests'])}"
    )
    out.append(f"- (a2) tests that run only common infrastructure lines: {len(res['common_only'])}")
    out.append(f"- (c) tests with zero unique lines: {len(res['tests_zero_unique'])}")
    out.append("")

    out.append("## (a) Tests covering no src lines (first 60)")
    out.append("")
    out += [f"- `{t}`" for t in no_cover[:60]] or ["(none)"]
    if len(no_cover) > 60:
        out.append(f"- ... and {len(no_cover) - 60} more (see JSON)")
    out.append("")

    out.append(
        "## (b1) Identical coverage across different test functions (narrow tests first, top 40)"
    )
    out.append("")
    for c in res["equal_clusters_cross_function"][:40]:
        out.append(
            f"- {len(c['members'])} tests, {c['lines']} lines each; "
            f"keeper suggestion `{c['keeper_suggestion']}`"
        )
        for m in c["members"][:6]:
            out.append(f"  - `{m}`")
        if len(c["members"]) > 6:
            out.append(f"  - ... {len(c['members']) - 6} more")
    out.append("")

    out.append("## (b2) Strict subset of another test's coverage (narrow tests first, top 60)")
    out.append("")
    out.append("`checks` = static assertion count; the superset shown is the closest one.")
    out.append("")
    for r in res["strict_subset_tests"][:60]:
        out.append(
            f"- `{r['test']}` ({r['lines']} lines, {r['checks']} checks) "
            f"within {r['n_supersets']} test(s); closest `{r['closest_superset']}` "
            f"({r['closest_superset_lines']} lines, {r['closest_superset_checks']} checks)"
        )
    out.append("")

    out.append("## (c) Zero unique lines (first 60 of the sorted list)")
    out.append("")
    uniq: dict[str, int] = res["unique"]
    out += [f"- `{t}`" for t in res["tests_zero_unique"][:60]] or ["(none)"]
    out.append("")
    low = sorted(uniq.items(), key=lambda kv: kv[1])
    out.append(
        f"Unique-line distribution: zero={sum(1 for _, n in low if n == 0)}, "
        f"1-3={sum(1 for _, n in low if 1 <= n <= 3)}, "
        f">3={sum(1 for _, n in low if n > 3)}."
    )
    out.append("")

    out.append("## (d) Slowest 50 tests")
    out.append("")
    if times:
        for t, s in sorted(times.items(), key=lambda kv: -kv[1])[:50]:
            out.append(f"- {s:7.2f}s `{t}`")
    else:
        out.append("(no JUnit XML given)")
    out.append("")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Per-test coverage redundancy report")
    ap.add_argument(
        "--data", nargs="+", type=Path, default=[Path(".local/test-audit/cov/combined")]
    )
    ap.add_argument("--junit", nargs="*", type=Path, default=None)
    ap.add_argument(
        "--static-json", type=Path, default=Path(".local/test-audit/static-report.json")
    )
    ap.add_argument("--out-dir", type=Path, default=Path(".local/test-audit"))
    ap.add_argument("--include-import-time", action="store_true")
    ap.add_argument(
        "--common-threshold",
        type=float,
        default=0.05,
        help="drop lines hit by more than this fraction of tests before the "
        "subset analysis (0 keeps everything)",
    )
    ap.add_argument(
        "--tag",
        default="",
        help="suffix for the report file names (coverage-report<tag>.md)",
    )
    ap.add_argument(
        "--phases",
        default="run",
        help="comma list of setup,run,teardown contexts to count (default: run)",
    )
    args = ap.parse_args(argv)

    junit = args.junit
    if junit is None:
        junit = sorted(Path(".local/test-audit/cov").glob("shard*.xml"))
    times, xml_ids = junit_info(list(junit)) if junit else ({}, set())

    stats: dict[str, list[int]] = {}
    if args.static_json.exists():
        stats = json.loads(args.static_json.read_text(encoding="utf-8")).get("stats", {})

    tests, _table = load_tests(
        list(args.data), args.include_import_time, frozenset(args.phases.split(","))
    )
    all_tests = set(tests)
    tests, n_common, common_only = drop_common_lines(tests, args.common_threshold)
    res = analyse(tests, stats)
    res["common_only"] = common_only
    note = (
        f"Common-line filter: {n_common} lines hit by more than "
        f"{args.common_threshold:.0%} of tests were ignored; {len(common_only)} tests "
        "covered nothing else (they only run shared infrastructure)."
    )
    no_cover = sorted(t for t in xml_ids if t not in all_tests) if xml_ids else []

    args.out_dir.mkdir(parents=True, exist_ok=True)
    md = render(res, note, no_cover, times, len(xml_ids))
    (args.out_dir / f"coverage-report{args.tag}.md").write_text(md, encoding="utf-8")
    payload = {k: v for k, v in res.items() if k != "unique"}
    payload["no_cover"] = no_cover
    payload["slowest"] = sorted(times.items(), key=lambda kv: -kv[1])[:50]
    payload["unique_counts"] = res["unique"]
    (args.out_dir / f"coverage-report{args.tag}.json").write_text(
        json.dumps(payload, indent=1), encoding="utf-8"
    )
    print(md.split("## (a)")[0])
    print(f"wrote {args.out_dir}/coverage-report{args.tag}.md and .json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
