"""Build an immutable, self-describing release directory from a KGX archive.

Implements F1 (packaging) and F2 (manifest) of ``docs/production-release-plan.md``.

The deployable unit is a directory whose contents are fixed once written:

    <version>/
      <graph>.csrgraph.pkl.zst
      <graph>.csrgraph.memmap/      # optional, for near-instant startup
      <graph>.metadata.lmdb/
      manifest.json                 # written last, inside the staging dir

Two properties make it safe to consume:

**The release appears atomically.** Everything is built in a staging directory
alongside the target and moved into place with a single ``os.replace`` once the
manifest is written. A consumer polling for ``<version>/`` therefore never sees a
partial release, and — the point of doing it this way —
``LMDBMetadataBackend.build()`` starts by ``rmtree``-ing its target, so aiming it
at a live directory would destroy the running store before producing a new one.
Rebuilding the stores in this repo had to be hand-worked around exactly that way.

**The release declares its store format.** ``store_format_version`` records the
key layout the stores were built with (see :data:`metadata_db.STORE_FORMAT_VERSION`).
A store built by older code is silently unreadable by newer code rather than
loudly broken, so this field is what lets a server refuse to serve instead of
answering every query with an empty result.

Usage::

    .venv/bin/python make_release.py ~/tmp/csrgraph_data/dgidb.tar.zst \\
        --version 2026-08-14 --graph-name dgidb --out-root ~/tmp/releases

    .venv/bin/python make_release.py <archive> --version 2026-07-19 \\
        --graph-name translator_kg_2026-07-19 --out-root /srv/csrgraph/releases

Costs scale with the graph. For the 28.9M-edge Translator KG, the LMDB store
dominates at roughly 52 minutes and 24 GB; the CSR snapshot is ~190 s and the
memmap another 3 s.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csrgraph_kgx import CSRGraph
from metadata_db import (
    STORE_FORMAT_VERSION,
    LMDBMetadataBackend,
    _stream_kgx,
    qualifier_fingerprint,
)

_HASH_CHUNK = 1 << 20


def _h64(text: str) -> int:
    """Stable 64-bit hash.

    Deliberately not the builtin ``hash()``: that is salted per process, so a
    release gate using it would report numbers that could not be reproduced by a
    second run. Cardinality would be unaffected, but a gate whose output is not
    reproducible is a gate nobody can check.
    """
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> tuple[str, int]:
    """Return ``(hex digest, byte size)`` for one file."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _sha256_tree(root: Path) -> tuple[str, int]:
    """Return ``(hex digest, total bytes)`` for a directory.

    Relative paths are walked in sorted order and mixed into the digest along
    with the contents, so the hash covers *layout* as well as bytes: a file
    renamed or moved changes it. Without the path in the digest, two different
    trees holding the same set of file contents would hash identically.
    """
    h = hashlib.sha256()
    total = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix().encode()
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        with open(path, "rb") as fh:
            while chunk := fh.read(_HASH_CHUNK):
                h.update(chunk)
                total += len(chunk)
    return h.hexdigest(), total


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

def _build_graph(archive: Path, dest: Path, build_memmap: bool) -> tuple[int, int, int]:
    """Build the CSR snapshot (and optional memmap) into *dest*.

    Returns ``(node_count, distinct_triples, source_records)``.  The last two are
    **different numbers** and conflating them is a trap:

    * ``CSRGraph.edge_count`` is ``len(normalized_triples)`` — the *raw record*
      count, before COO merges repeated coordinates.
    * The per-predicate CSR matrices, being built from COO, hold only *distinct*
      ``(subject, predicate, object)``. That is the sum of their ``nnz``.

    On dgidb those are 52,065 and 51,943; on the 2026-07-19 Translator KG,
    28,925,258 and 28,105,517. The manifest records both, because the sensible
    completeness check on ``variant_count`` is bounded by them on either side
    (see :func:`_verify`).
    """
    print(f"[1/3] CSR snapshot from {archive.name} ...", flush=True)
    t0 = time.perf_counter()
    graph = CSRGraph.from_kgx_archive(str(archive))
    nodes = graph.num_nodes
    records = graph.edge_count                                    # raw, pre-merge
    distinct = sum(c.nnz for c in graph.csr_by_relation.values())  # post-merge
    graph.save(str(dest))
    print(f"      {nodes:,} nodes, {distinct:,} distinct triples "
          f"from {records:,} records ({time.perf_counter() - t0:.0f}s)", flush=True)

    if build_memmap:
        print("[2/3] memmap ...", flush=True)
        t0 = time.perf_counter()
        graph._to_memmap(CSRGraph._memmap_dir(str(dest)))
        print(f"      done ({time.perf_counter() - t0:.0f}s)", flush=True)
    else:
        print("[2/3] memmap skipped (--no-memmap)", flush=True)
    return nodes, distinct, records


def _build_lmdb(archive: Path, dest: Path, node_fields: list[str],
                edge_fields: list[str]) -> int:
    """Build the LMDB metadata store into *dest*; return its variant count."""
    print(f"[3/3] LMDB metadata (the long step) ...", flush=True)
    t0 = time.perf_counter()
    db = LMDBMetadataBackend.build(
        str(archive), str(dest),
        node_metadata_fields=node_fields, edge_metadata_fields=edge_fields,
    )
    try:
        with db._env.begin(db=db._edges_db) as txn:
            variants = txn.stat(db=db._edges_db)["entries"]
    finally:
        db.close()
    print(f"      {variants:,} edge variants ({time.perf_counter() - t0:.0f}s)",
          flush=True)
    return variants


def _gate_completeness(archive: Path, distinct: int, variants: int) -> dict:
    """F5: count the source independently and require the stores to match it.

    The bound in :func:`_verify` is a sanity check between two numbers the build
    itself produced; this derives the truth from the archive again and compares.
    It is what established that the pre-version-2 key was dropping 754,788
    assertions, and what confirmed LMDB, Elasticsearch and the source agreeing at
    exactly 28,860,305.

    Costs one extra streaming pass over the archive — minutes on the Translator
    KG, instant on a sample graph. 64-bit hashes are kept rather than keys: 29M
    ``uint64`` is 231 MB where 29M Python strings in a set would be several GB,
    and at that scale a collision has probability ~2e-5.
    """
    print("      gate: recounting the source ...", flush=True)
    t0 = time.perf_counter()
    tri: set[int] = set()
    var: set[int] = set()
    graph_meta: dict = {}
    for kind, rec in _stream_kgx(str(archive), include_metadata=True):
        if kind == "graph_metadata":
            # Free here: this pass already decompresses the whole archive, and
            # graph-metadata.json is its last member.
            graph_meta = rec
            continue
        if kind != "edge":
            continue
        spo = f"{rec['subject']}|{rec['predicate']}|{rec['object']}"
        tri.add(_h64(spo))
        var.add(_h64(f"{spo}|{qualifier_fingerprint(rec)}"))
    print(f"      source has {len(tri):,} distinct triples, {len(var):,} variants "
          f"({time.perf_counter() - t0:.0f}s)", flush=True)

    if len(tri) != distinct:
        raise SystemExit(
            f"refusing to publish: the archive holds {len(tri):,} distinct triples "
            f"but the graph holds {distinct:,}. The CSR snapshot is not a faithful "
            "copy of the source."
        )
    if len(var) != variants:
        raise SystemExit(
            f"refusing to publish: the archive holds {len(var):,} edge variants but "
            f"the metadata store holds {variants:,}. "
            + ("The store is dropping assertions — the symptom of a store built "
               "with a key that ignores qualifiers." if variants < len(var)
               else "The store has more records than the source, which cannot happen.")
        )
    return graph_meta


def _gate_corpus(release_dir: Path, graph_name: str) -> None:
    """F5: run the data-gated corpus invariants against the staged release.

    Skips cleanly when ``trapi_corpus`` is not importable, since the HelmsDeep
    corpus lives outside this repo and only means anything on a
    Translator-shaped graph. When it does run it is the strongest gate available:
    every accuracy regression in this project's history was caught by the corpus
    and by nothing else.
    """
    import subprocess

    try:
        import trapi_corpus  # noqa: F401
    except ImportError:
        print("      gate: corpus skipped (trapi_corpus not importable)", flush=True)
        return
    print("      gate: corpus invariants ...", flush=True)
    env = {**os.environ, "DATA_DIR": str(release_dir), "GRAPH_NAME": graph_name}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         str(Path(__file__).resolve().parent / "tests" / "test_corpus.py")],
        env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-15:])
        raise SystemExit(f"refusing to publish: corpus gate failed\n{tail}")
    print(f"      {proc.stdout.strip().splitlines()[-1]}", flush=True)


def _verify(lmdb_dir: Path, graph_path: Path, distinct: int, records: int,
            variants: int) -> None:
    """Check the release is internally consistent and readable by *this* code.

    Cheap, and it catches the failure this whole design exists to prevent: a
    store whose key layout the running code cannot read. Reading one edge back
    is enough — under a format mismatch every variant lookup returns empty.

    The completeness check is two-sided, and getting it wrong the obvious way is
    easy: ``variants`` is legitimately *below* the raw record count, because
    duplicate records with identical qualifiers collapse to one variant
    (28,860,305 from 28,925,258 records on the Translator KG). Only the distinct
    triple count is a true lower bound.
    """
    if variants < distinct:
        raise SystemExit(
            f"refusing to publish: {variants:,} edge variants is fewer than "
            f"{distinct:,} distinct triples, which is impossible — every triple "
            "has at least one variant. The metadata store is incomplete."
        )
    if variants > records:
        raise SystemExit(
            f"refusing to publish: {variants:,} edge variants exceeds the "
            f"{records:,} records in the source, which cannot happen."
        )

    graph = CSRGraph.load(str(graph_path))
    db = LMDBMetadataBackend(str(lmdb_dir))
    try:
        probe = None
        for rel, csr in graph.csr_by_relation.items():
            if csr.nnz:
                row = next(i for i in range(csr.shape[0])
                           if csr.indptr[i + 1] > csr.indptr[i])
                probe = (graph.nodes[row], f"biolink:{rel}",
                         graph.nodes[int(csr.indices[csr.indptr[row]])])
                break
        if probe is None:
            raise SystemExit("refusing to publish: graph has no edges")
        if not db.get_edge_variants(*probe):
            raise SystemExit(
                f"refusing to publish: no metadata for {probe[0]} -{probe[1]}-> "
                f"{probe[2]}, an edge the graph contains. The store is unreadable "
                f"by this code (expected store format {STORE_FORMAT_VERSION})."
            )
        print(f"      verified: metadata readable for {probe[0]} "
              f"-{probe[1].split(':')[1]}-> {probe[2]}", flush=True)
    finally:
        db.close()


def _manifest(*, graph_name: str, version: str, archive: Path,
              graph_path: Path, lmdb_dir: Path, memmap_dir: Path | None,
              nodes: int, distinct: int, records: int, variants: int,
              graph_meta: dict | None = None) -> dict:
    """Assemble the F2 manifest, hashing every artifact in the staging dir."""
    print("      hashing artifacts ...", flush=True)
    pkl_hash, pkl_bytes = _sha256_file(graph_path)
    lmdb_hash, lmdb_bytes = _sha256_tree(lmdb_dir)
    artifacts = {
        "pkl_zst": {"path": graph_path.name, "sha256": pkl_hash, "bytes": pkl_bytes},
        "lmdb": {"path": lmdb_dir.name, "sha256_tree": lmdb_hash, "bytes": lmdb_bytes},
    }
    if memmap_dir is not None and memmap_dir.exists():
        mm_hash, mm_bytes = _sha256_tree(memmap_dir)
        artifacts["memmap"] = {"path": memmap_dir.name, "sha256_tree": mm_hash,
                               "bytes": mm_bytes}
    src_hash, src_bytes = _sha256_file(archive)
    return {
        "graph_name": graph_name,
        "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_kgx": archive.name,
        "source_sha256": src_hash,
        "source_bytes": src_bytes,
        "store_format_version": STORE_FORMAT_VERSION,
        # The Biolink version the source was normalised against. Recorded so
        # predicate expansion can be pinned to it: BiolinkExpander resolving
        # against a different model than the data was built with is a silent
        # correctness gap, not an error. None when --no-gate-completeness skipped
        # the pass that reads it.
        "biolink_version": (graph_meta or {}).get("biolinkVersion"),
        "artifacts": artifacts,
        "node_count": nodes,
        "edge_count": distinct,            # distinct (subject, predicate, object)
        "source_record_count": records,     # raw edge records in the archive
        "variant_count": variants,          # distinct (s, p, o, qualifier fingerprint)
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", help="source KGX .tar.zst")
    ap.add_argument("--version", required=True,
                    help="release version, used as the directory name (e.g. 2026-07-19)")
    ap.add_argument("--graph-name", required=True,
                    help="graph stem; artifacts are named <graph-name>.*")
    ap.add_argument("--out-root", required=True,
                    help="parent directory that will contain <version>/")
    ap.add_argument("--no-memmap", action="store_true",
                    help="skip the memmap dir (slower startup, ~745 MB smaller)")
    ap.add_argument("--node-metadata-fields", default="all",
                    help="comma-separated, or 'all' (default)")
    ap.add_argument("--edge-metadata-fields", default="all",
                    help="comma-separated, or 'all' (default)")
    ap.add_argument("--skip-verify", action="store_true",
                    help="skip the readability check (not recommended)")
    ap.add_argument("--no-gate-completeness", action="store_true",
                    help="skip recounting the archive (saves one streaming pass)")
    ap.add_argument("--gate-corpus", action="store_true",
                    help="also run tests/test_corpus.py against the staged release")
    ap.add_argument("--force", action="store_true",
                    help="replace an existing release of this version")
    a = ap.parse_args()

    archive = Path(a.archive).expanduser().resolve()
    if not archive.exists():
        raise SystemExit(f"archive not found: {archive}")
    out_root = Path(a.out_root).expanduser().resolve()
    target = out_root / a.version
    if target.exists() and not a.force:
        raise SystemExit(f"release already exists: {target}  (pass --force to replace)")

    # Stage beside the target, on the same filesystem, so the publish is a rename.
    stage = out_root / f".{a.version}.staging.{os.getpid()}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    fields = lambda spec: ["all"] if spec == "all" else [f for f in spec.split(",") if f]

    t_start = time.perf_counter()
    try:
        graph_path = stage / f"{a.graph_name}.csrgraph.pkl.zst"
        lmdb_dir = stage / f"{a.graph_name}.metadata.lmdb"
        nodes, distinct, records = _build_graph(archive, graph_path, not a.no_memmap)
        variants = _build_lmdb(archive, lmdb_dir,
                               fields(a.node_metadata_fields),
                               fields(a.edge_metadata_fields))
        if not a.skip_verify:
            _verify(lmdb_dir, graph_path, distinct, records, variants)
        graph_meta: dict = {}
        if not a.no_gate_completeness:
            graph_meta = _gate_completeness(archive, distinct, variants)
        if a.gate_corpus:
            _gate_corpus(stage, a.graph_name)

        manifest = _manifest(
            graph_name=a.graph_name, version=a.version, archive=archive,
            graph_path=graph_path, lmdb_dir=lmdb_dir,
            memmap_dir=None if a.no_memmap else CSRGraph._memmap_dir(str(graph_path)),
            nodes=nodes, distinct=distinct, records=records, variants=variants,
            graph_meta=graph_meta,
        )
        # Last file written, so the atomic move below publishes a complete release.
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

        if target.exists():
            doomed = out_root / f".{a.version}.replaced.{os.getpid()}"
            os.replace(target, doomed)
            os.replace(stage, target)
            shutil.rmtree(doomed, ignore_errors=True)
        else:
            os.replace(stage, target)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    total = sum(v.get("bytes", 0) for v in manifest["artifacts"].values())
    print(f"\npublished {target}")
    print(f"  store_format_version {manifest['store_format_version']}   "
          f"{nodes:,} nodes   {distinct:,} triples   {variants:,} variants "
          f"(from {records:,} records)")
    print(f"  {total / 1e9:.2f} GB in {time.perf_counter() - t_start:.0f}s")


if __name__ == "__main__":
    main()
