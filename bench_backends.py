#!/usr/bin/env python3
"""Benchmark: compare storage size and query performance across all four MetadataBackend
implementations (SQLite, DuckDB, LMDB, Elasticsearch).

Usage
-----
    # Default: uses dgidb.tar.zst in the standard data directory
    python csrgraph/bench_backends.py

    # Custom archive
    python csrgraph/bench_backends.py /path/to/kg.tar.zst

    # Skip Elasticsearch (requires a running server at localhost:9200)
    python csrgraph/bench_backends.py --skip-es

    # Use a specific ES host
    python csrgraph/bench_backends.py --es-host http://localhost:9200

    # Control how many times each query is repeated for timing
    python csrgraph/bench_backends.py --reps 200

    # Compare multiple bulk sizes in one run (builds each backend once)
    python csrgraph/bench_backends.py --bulk-sizes 50 100 500 1000

    # Keep temp DB files for inspection
    python csrgraph/bench_backends.py --keep-tmp

Output
------
Prints a point-lookup table (get_node / get_edge) and one filter table per
bulk size, all backends as rows.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve the csrgraph package directory so this script can be run from
# the repo root without installing anything.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent.resolve()
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from metadata_db import (  # noqa: E402
    DuckDBMetadataBackend,
    ElasticsearchMetadataBackend,
    LMDBMetadataBackend,
    SQLiteMetadataBackend,
    _stream_kgx,
)

# ---------------------------------------------------------------------------
# Tee: write to stdout and a file simultaneously
# ---------------------------------------------------------------------------

class _Tee(io.TextIOBase):
    """Wraps sys.stdout so every write goes to both the terminal and a file."""

    def __init__(self, path: str) -> None:
        self._file = open(path, "w", encoding="utf-8")
        self._stdout = sys.stdout

    def write(self, s: str) -> int:
        self._stdout.write(s)
        self._file.write(s)
        return len(s)

    def flush(self) -> None:
        self._stdout.flush()
        self._file.flush()

    def close(self) -> None:
        sys.stdout = self._stdout
        self._file.close()


# ---------------------------------------------------------------------------
# Default data path
# ---------------------------------------------------------------------------
_DEFAULT_DATA_DIR = Path(
    os.environ.get("CSRGRAPH_DATA_DIR", "~/tmp/csrgraph_data")
).expanduser()
_DEFAULT_ARCHIVE = _DEFAULT_DATA_DIR / "dgidb.tar.zst"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dir_size(path: str) -> int:
    total = 0
    for entry in os.scandir(path):
        if entry.is_file(follow_symlinks=False):
            total += entry.stat().st_size
    return total


def _file_size(path: str) -> int:
    return Path(path).stat().st_size


def _timeit(fn, *, reps: int) -> tuple[float, float]:
    """Return (mean_ms, stdev_ms) over *reps* calls to *fn*."""
    times: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    mean  = statistics.mean(times)
    stdev = statistics.stdev(times) if len(times) > 1 else 0.0
    return mean, stdev


def _sample_ids(archive_path: str, max_nodes: int, max_edges: int):
    """Stream the archive once and collect real node / edge IDs."""
    node_ids: list[str] = []
    edge_tuples: list[tuple[str, str, str]] = []
    for kind, rec in _stream_kgx(archive_path):
        if kind == "node" and len(node_ids) < max_nodes:
            node_ids.append(rec["id"])
        elif kind == "edge" and len(edge_tuples) < max_edges:
            edge_tuples.append((rec["subject"], rec["predicate"], rec["object"]))
        if len(node_ids) >= max_nodes and len(edge_tuples) >= max_edges:
            break
    return node_ids, edge_tuples


def _fmt_size(nbytes: int) -> str:
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024**3:.2f} GB"
    if nbytes >= 1024 ** 2:
        return f"{nbytes / 1024**2:.1f} MB"
    return f"{nbytes / 1024:.1f} KB"


def _fmt_ms(mean: float, stdev: float) -> str:
    return f"{mean:.3f} ± {stdev:.3f}"


def _print_md_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    print("| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths)) + " |")
    print(sep)
    for row in rows:
        print("| " + " | ".join(v.ljust(w) for v, w in zip(row, widths)) + " |")


# ---------------------------------------------------------------------------
# Per-backend benchmark runner
# ---------------------------------------------------------------------------

def _run_backend(
    name: str,
    build_fn,
    size_fn,
    node_ids: list[str],
    edge_tuples: list[tuple[str, str, str]],
    bulk_sizes: list[int],
    *,
    reps: int,
) -> dict:
    """Build the backend once, benchmark point lookups and all bulk sizes."""
    print(f"\n{'='*60}")
    print(f"  Backend: {name}")
    print(f"{'='*60}")

    t0 = time.perf_counter()
    backend, db_path = build_fn()
    build_elapsed = time.perf_counter() - t0
    size_bytes = size_fn(db_path)

    print(f"  build time  : {build_elapsed:.1f}s")
    print(f"  size on disk: {_fmt_size(size_bytes)}")

    result: dict = {
        "name":       name,
        "build_s":    build_elapsed,
        "size_bytes": size_bytes,
        "filters":    {},
    }

    try:
        probe_node = next((nid for nid in node_ids if backend.get_node(nid)), node_ids[0])
        probe_subj, probe_pred, probe_obj = edge_tuples[0]

        # Warm up
        for _ in range(5):
            backend.get_node(probe_node)
            backend.get_edge(probe_subj, probe_pred, probe_obj)

        # Point lookups (independent of bulk size)
        gn_mean, gn_std = _timeit(lambda: backend.get_node(probe_node), reps=reps)
        ge_mean, ge_std = _timeit(
            lambda: backend.get_edge(probe_subj, probe_pred, probe_obj), reps=reps
        )
        print(f"  get_node : {_fmt_ms(gn_mean, gn_std)} ms  (n={reps})")
        print(f"  get_edge : {_fmt_ms(ge_mean, ge_std)} ms  (n={reps})")
        result["get_node"] = (gn_mean, gn_std)
        result["get_edge"] = (ge_mean, ge_std)

        # Filter benchmarks at each bulk size
        for bsz in bulk_sizes:
            fn_ids    = node_ids[:bsz]
            fe_edges: list[tuple[str, str | None, str]] = [
                (s, p, o) for s, p, o in edge_tuples[:bsz]
            ]

            fn_mean,  fn_std  = _timeit(lambda: backend.filter_nodes(fn_ids), reps=reps)
            fnc_mean, fnc_std = _timeit(
                lambda: backend.filter_nodes(fn_ids, category="biolink:Gene"), reps=reps
            )
            fe_mean,  fe_std  = _timeit(lambda: backend.filter_edges(fe_edges), reps=reps)
            fek_mean, fek_std = _timeit(
                lambda: backend.filter_edges(fe_edges, knowledge_level="knowledge_assertion"),
                reps=reps,
            )

            print(
                f"  bulk={bsz:>4} | "
                f"fn={fn_mean:.3f} fn+cat={fnc_mean:.3f} "
                f"fe={fe_mean:.3f} fe+kl={fek_mean:.3f} ms"
            )
            result["filters"][bsz] = {
                "filter_nodes":     (fn_mean,  fn_std),
                "filter_nodes_cat": (fnc_mean, fnc_std),
                "filter_edges":     (fe_mean,  fe_std),
                "filter_edges_kl":  (fek_mean, fek_std),
            }

    finally:
        backend.close()

    return result


# ---------------------------------------------------------------------------
# Summary tables
# ---------------------------------------------------------------------------

def _print_summary(results: list[dict], bulk_sizes: list[int]) -> None:
    print("\n\n" + "=" * 80)
    print("POINT LOOKUPS")
    print("=" * 80)
    _print_md_table(
        ["Backend", "Build (s)", "Size", "get_node (ms)", "get_edge (ms)"],
        [
            [
                r["name"],
                f"{r['build_s']:.1f}",
                _fmt_size(r["size_bytes"]),
                f"{r['get_node'][0]:.3f} ± {r['get_node'][1]:.3f}",
                f"{r['get_edge'][0]:.3f} ± {r['get_edge'][1]:.3f}",
            ]
            for r in results
        ],
    )

    for bsz in bulk_sizes:
        print(f"\n{'='*80}")
        print(f"FILTER QUERIES  bulk={bsz}")
        print("=" * 80)
        _print_md_table(
            [
                "Backend",
                f"filter_nodes/{bsz} (ms)",
                f"filter_nodes/{bsz}+cat (ms)",
                f"filter_edges/{bsz} (ms)",
                f"filter_edges/{bsz}+kl (ms)",
            ],
            [
                [
                    r["name"],
                    f"{r['filters'][bsz]['filter_nodes'][0]:.3f} ± {r['filters'][bsz]['filter_nodes'][1]:.3f}",
                    f"{r['filters'][bsz]['filter_nodes_cat'][0]:.3f} ± {r['filters'][bsz]['filter_nodes_cat'][1]:.3f}",
                    f"{r['filters'][bsz]['filter_edges'][0]:.3f} ± {r['filters'][bsz]['filter_edges'][1]:.3f}",
                    f"{r['filters'][bsz]['filter_edges_kl'][0]:.3f} ± {r['filters'][bsz]['filter_edges_kl'][1]:.3f}",
                ]
                for r in results
                if bsz in r["filters"]
            ],
        )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "archive", nargs="?", default=str(_DEFAULT_ARCHIVE),
        help="Path to KGX .tar.zst archive (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-sqlite", action="store_true",
        help="Skip SQLite backend",
    )
    parser.add_argument(
        "--skip-duckdb", action="store_true",
        help="Skip DuckDB backend",
    )
    parser.add_argument(
        "--skip-lmdb", action="store_true",
        help="Skip LMDB backend",
    )
    parser.add_argument(
        "--skip-es", action="store_true",
        help="Skip Elasticsearch backend (requires a running server)",
    )
    parser.add_argument(
        "--es-host", default="http://localhost:9200",
        help="Elasticsearch host URL (default: %(default)s)",
    )
    parser.add_argument(
        "--es-prefix", default="bench_kg",
        help="Elasticsearch index prefix (default: %(default)s)",
    )
    parser.add_argument(
        "--es-timeout", type=float, default=120.0,
        help="ES per-request timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--es-chunk-size", type=int, default=500,
        help="ES bulk chunk size — docs per HTTP request (default: %(default)s)",
    )
    parser.add_argument(
        "--reps", type=int, default=100,
        help="Number of timing repetitions per query (default: %(default)s)",
    )
    parser.add_argument(
        "--bulk-sizes", type=int, nargs="+", default=[50],
        metavar="N",
        help="One or more bulk sizes for filter benchmarks (default: 50)",
    )
    parser.add_argument(
        "--keep-tmp", action="store_true",
        help="Keep the temp directory with all built DB files after the run",
    )
    parser.add_argument(
        "--report", metavar="FILE",
        help="Save a copy of all output to FILE (in addition to stdout)",
    )
    args = parser.parse_args()

    tee = _Tee(args.report) if args.report else None
    if tee:
        sys.stdout = tee

    archive = args.archive
    if not Path(archive).exists():
        print(f"ERROR: archive not found: {archive}", file=sys.stderr)
        sys.exit(1)

    bulk_sizes = sorted(set(args.bulk_sizes))
    max_bulk   = bulk_sizes[-1]

    print(f"Archive    : {archive}")
    print(f"Reps       : {args.reps}")
    print(f"Bulk sizes : {bulk_sizes}")

    # Sample enough IDs to cover the largest bulk size
    print("\nSampling node/edge IDs from archive ...")
    node_ids, edge_tuples = _sample_ids(archive, max_nodes=max_bulk, max_edges=max_bulk)
    if len(node_ids) < max_bulk or len(edge_tuples) < max_bulk:
        print(
            f"WARNING: archive has only {len(node_ids)} nodes / {len(edge_tuples)} edges; "
            f"reducing bulk sizes accordingly."
        )
        bulk_sizes = [b for b in bulk_sizes if b <= min(len(node_ids), len(edge_tuples))]
        if not bulk_sizes:
            print("ERROR: no valid bulk sizes remain.", file=sys.stderr)
            sys.exit(1)
    print(f"  sampled {len(node_ids)} nodes, {len(edge_tuples)} edges")

    tmpdir = tempfile.mkdtemp(prefix="kgbench_")
    print(f"\nTemp dir: {tmpdir}")

    results: list[dict] = []

    try:
        # ── SQLite ────────────────────────────────────────────────────────────
        if args.skip_sqlite:
            print("\nSkipping SQLite backend (--skip-sqlite)")
        else:
            sqlite_path = os.path.join(tmpdir, "bench.sqlite")
            results.append(_run_backend(
                "SQLite",
                lambda: (
                    SQLiteMetadataBackend.build(
                        archive, sqlite_path,
                        node_metadata_fields=["all"],
                        edge_metadata_fields=["all"],
                    ),
                    sqlite_path,
                ),
                _file_size,
                node_ids, edge_tuples, bulk_sizes, reps=args.reps,
            ))

        # ── DuckDB ───────────────────────────────────────────────────────────
        if args.skip_duckdb:
            print("\nSkipping DuckDB backend (--skip-duckdb)")
        else:
            duckdb_path = os.path.join(tmpdir, "bench.duckdb")
            results.append(_run_backend(
                "DuckDB",
                lambda: (
                    DuckDBMetadataBackend.build(
                        archive, duckdb_path,
                        node_metadata_fields=["all"],
                        edge_metadata_fields=["all"],
                    ),
                    duckdb_path,
                ),
                _file_size,
                node_ids, edge_tuples, bulk_sizes, reps=args.reps,
            ))

        # ── LMDB ─────────────────────────────────────────────────────────────
        if args.skip_lmdb:
            print("\nSkipping LMDB backend (--skip-lmdb)")
        else:
            lmdb_path = os.path.join(tmpdir, "bench.lmdb")
            results.append(_run_backend(
                "LMDB",
                lambda: (
                    LMDBMetadataBackend.build(
                        archive, lmdb_path,
                        node_metadata_fields=["all"],
                        edge_metadata_fields=["all"],
                    ),
                    lmdb_path,
                ),
                _dir_size,
                node_ids, edge_tuples, bulk_sizes, reps=args.reps,
            ))

        # ── Elasticsearch ─────────────────────────────────────────────────────
        if args.skip_es:
            print("\nSkipping Elasticsearch backend (--skip-es)")
        else:
            try:
                import elasticsearch  # noqa: F401
            except ImportError:
                print("\nSkipping Elasticsearch: `pip install elasticsearch` required")
            else:
                def _build_es():
                    db = ElasticsearchMetadataBackend.build(
                        archive,
                        host=args.es_host,
                        index_prefix=args.es_prefix,
                        node_metadata_fields=["all"],
                        edge_metadata_fields=["all"],
                        request_timeout=args.es_timeout,
                        bulk_chunk_size=args.es_chunk_size,
                    )
                    try:
                        stats = db._es.indices.stats(
                            index=f"{args.es_prefix}_*", metric="store"
                        )
                        total = sum(
                            v["total"]["store"]["size_in_bytes"]
                            for v in stats["indices"].values()
                        )
                    except Exception:
                        total = 0
                    return db, total

                results.append(_run_backend(
                    "Elasticsearch", _build_es, lambda x: x,
                    node_ids, edge_tuples, bulk_sizes, reps=args.reps,
                ))

    finally:
        if args.keep_tmp:
            print(f"\nTemp dir kept (--keep-tmp): {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)
            print(f"\nCleaned up temp dir: {tmpdir}")

    _print_summary(results, bulk_sizes)

    if tee:
        tee.close()
        print(f"Report saved to: {args.report}", file=tee._stdout)


if __name__ == "__main__":
    main()
