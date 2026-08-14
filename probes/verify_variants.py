"""Ground truth for qualifier-variant keying on the full archive.

Counts distinct ``(subject, predicate, object)`` and distinct
``(subject, predicate, object, qualifier_fingerprint)`` in the source KGX edges,
so the rebuilt LMDB and Elasticsearch stores can be checked against a number
derived independently of them. The difference between the two counts is exactly
the number of assertions the old triple-keyed stores were dropping.

Reads the *extracted* ``edges.jsonl`` (gandalf needs it uncompressed anyway) in
parallel byte ranges, and keeps 64-bit hashes rather than the keys themselves:
29M keys as a Python set costs several GB, whereas 29M ``uint64`` is 231 MB. At
that scale a 64-bit collision has probability ~2e-5, far below the precision
this check needs.

    .venv/bin/python probes/verify_variants.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metadata_db import qualifier_fingerprint  # noqa: E402

EDGES = os.path.expanduser("~/tmp/gandalf_data/kgx_2026-07-19/edges.jsonl")
WORKERS = 12


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")


def _scan(args) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Hash every edge in [start, end) — aligned to line boundaries.

    Also hashes the *whole* record, so the records that stay collapsed under
    ``(s, p, o, fingerprint)`` can be checked for whether they are genuinely
    identical or differ somewhere the fingerprint ignores.
    """
    start, end = args
    triples: list[int] = []
    variants: list[int] = []
    wholes: list[int] = []
    n = 0
    with open(EDGES, "rb") as fh:
        if start:
            fh.seek(start - 1)
            fh.readline()  # discard the line straddling the boundary
        else:
            fh.seek(0)
        while fh.tell() < end:
            line = fh.readline()
            if not line:
                break
            try:
                d = json.loads(line)
            except ValueError:
                continue
            spo = f"{d['subject']}|{d['predicate']}|{d['object']}"
            triples.append(_h(spo))
            variants.append(_h(f"{spo}|{qualifier_fingerprint(d)}"))
            wholes.append(_h(json.dumps(d, sort_keys=True, default=str)))
            n += 1
    return (np.array(triples, dtype=np.uint64),
            np.array(variants, dtype=np.uint64),
            np.array(wholes, dtype=np.uint64), n)


def main() -> None:
    size = os.path.getsize(EDGES)
    step = size // WORKERS
    ranges = [(i * step, size if i == WORKERS - 1 else (i + 1) * step)
              for i in range(WORKERS)]

    with Pool(WORKERS) as pool:
        parts = pool.map(_scan, ranges)

    raw = sum(p[3] for p in parts)
    th = np.concatenate([p[0] for p in parts])
    vh = np.concatenate([p[1] for p in parts])
    wh = np.concatenate([p[2] for p in parts])

    triples = np.unique(th)
    variants = np.unique(vh)

    print(f"raw edge records              {raw:>12,}")
    print(f"distinct (s,p,o)              {len(triples):>12,}")
    print(f"distinct (s,p,o,fingerprint)  {len(variants):>12,}")
    print(f"assertions the old key lost   {len(variants) - len(triples):>12,}")
    print(f"exact-duplicate records       {raw - len(variants):>12,}")

    # Variants per triple: dedupe (triple, variant) pairs, then count per triple.
    # This is the number that has to bound any per-triple fetch size.
    pair = np.unique(np.stack([th, vh], axis=1), axis=0)
    counts = np.unique(pair[:, 0], return_counts=True)[1]
    hist = np.bincount(counts)
    print("\nvariants per triple")
    print(f"  max                         {counts.max():>12,}")
    print(f"  mean                        {counts.mean():>12.4f}")
    print(f"  single-variant triples      {hist[1]:>12,}  ({hist[1]/len(counts):.2%})")
    for n in (2, 3, 5, 10, 50, 100):
        over = int((counts >= n).sum())
        print(f"  triples with >= {n:<4}        {over:>12,}")

    # Are the still-collapsed records genuinely identical, or do they differ
    # somewhere the qualifier fingerprint ignores (sources, attributes)?  If any
    # variant key maps to more than one whole-record hash, the fingerprint is
    # losing distinguishable assertions and the docstring must not claim
    # otherwise.
    vw = np.unique(np.stack([vh, wh], axis=1), axis=0)
    per_variant = np.unique(vw[:, 0], return_counts=True)[1]
    ambiguous = int((per_variant > 1).sum())
    print("\nrecords still collapsed under (s,p,o,fingerprint)")
    print(f"  distinct whole records        {len(np.unique(wh)):>12,}")
    print(f"  variant keys holding >1       {ambiguous:>12,}")
    if ambiguous:
        print(f"  max distinct records/key      {per_variant.max():>12,}")
        print("  -> some collapsed records differ OUTSIDE their qualifiers")
    else:
        print("  -> every collapsed record is byte-identical; nothing lost")


if __name__ == "__main__":
    main()
