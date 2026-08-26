# Where the KGX source data comes from

This repo is code only — graph snapshots are not committed. Upstream KGX archives
come from the Translator ingest storage:

**<https://kgx-storage.ci.transltr.io/releases/>**

Source: [NCATSTranslator/translator-ingests](https://github.com/NCATSTranslator/translator-ingests).

## Layout

```
/releases/                               31 sources + latest-release-summary.json
/releases/<source>/                      version dirs + latest/ + latest-release.json
/releases/<source>/<version>/<source>.tar.zst
/releases/<source>/latest/<source>.tar.zst
```

Sources include the merged `translator_kg` and `translator_kg_open`, plus the
individual ingests: `dgidb`, `ttd`, `chembl`, `ctd`, `drugcentral`, `goa`, `hpoa`,
`intact`, `pubtator`, `semmeddb`, `string`, `ubergraph`, and others.

## Finding the latest release

One request describes every source — the fastest way to see what is current:

```bash
curl -s https://kgx-storage.ci.transltr.io/releases/latest-release-summary.json \
  | python3 -m json.tool | head -40
```

Each entry carries `source_version`, `release_date`, `biolink_version`,
`build_version`, `babel_version` and a `data` URL pointing at the pinned version
directory. Per-source detail is at
`/releases/<source>/latest-release.json`.

`HEAD` the archive to size a download before starting it:

```bash
curl -sIL https://kgx-storage.ci.transltr.io/releases/translator_kg/latest/translator_kg.tar.zst \
  | grep -iE 'content-length|last-modified'
```

> **`latest/` is a moving pointer.** It is right for "get me current" and wrong
> for anything reproducible — the bytes behind it change without the URL
> changing. Take the version from `latest-release.json` and download the pinned
> `/<version>/` path when the build has to be repeatable. `make_release.py`
> records `source_sha256` in the manifest either way, so a release directory can
> always be traced back to the exact bytes it was built from.

## Getting a graph from scratch

```bash
# 1. download (name it for its vintage, not "latest")
curl -L -o ~/tmp/csrgraph_data/translator_kg_2026-07-19.tar.zst \
  https://kgx-storage.ci.transltr.io/releases/translator_kg/latest/translator_kg.tar.zst

# 2. build an immutable release directory (CSR snapshot + memmap + LMDB + manifest)
.venv/bin/python make_release.py ~/tmp/csrgraph_data/translator_kg_2026-07-19.tar.zst \
    --version 2026-07-19 --graph-name translator_kg_2026-07-19 --out-root ~/tmp/releases

# 3. optional: Elasticsearch indices, needed only for resolve/full-text
.venv/bin/python -c "
from metadata_db import ElasticsearchMetadataBackend as E
E.build('$HOME/tmp/csrgraph_data/translator_kg_2026-07-19.tar.zst',
        host='http://localhost:9200', index_prefix='translator_kg_2026-07-19',
        node_metadata_fields=['all'], edge_metadata_fields=['all'],
        number_of_replicas=0)"
```

Step 3 has two traps that both present as *empty results rather than errors*:

- **`node_metadata_fields` / `edge_metadata_fields` are required in practice.**
  They default to `None`, and `load_nodes` / `load_edges` are derived as
  `... is not None`, so omitting them indexes *nothing* silently.
  `make_release.py` defaults them to `all`; the library does not.
- **Nothing is searchable until the build finishes.** It holds
  `refresh_interval: -1` for the bulk load, so mid-build `_count` is 0 while
  `_cat/indices` shows millions of docs.

Also pass `number_of_replicas=0` on a single node, or a replica stays unassigned
and cluster health never leaves yellow.

Measured throughput ~19,000 docs/s: the full Translator KG (~30.7M docs) takes
~28 min; the LMDB store for the same graph takes ~75 min and 24 GB.

## Refreshing the small fixtures

`dgidb` and `ttd` are the repo's cheap test graphs — `bench_backends.py` defaults
to `dgidb.tar.zst`, `make_release.py`'s docstring example uses `dgidb`, and
`trapi_server.py --graph dgidb` works. They live *flat* in `DATA_DIR` rather than
as release directories, because that is where those callers look. Rebuild them by
staging a release and moving the artifacts over (seconds each):

```bash
for s in dgidb ttd; do
  curl -sL -o ~/tmp/csrgraph_data/$s.tar.zst \
    https://kgx-storage.ci.transltr.io/releases/$s/latest/$s.tar.zst
  .venv/bin/python make_release.py ~/tmp/csrgraph_data/$s.tar.zst \
      --version <version> --graph-name $s --out-root ~/tmp/releases/_fixtures-$s
  for a in $s.csrgraph.pkl.zst $s.metadata.lmdb $s.csrgraph.memmap; do
    rm -rf ~/tmp/csrgraph_data/$a
    mv ~/tmp/releases/_fixtures-$s/<version>/$a ~/tmp/csrgraph_data/$a
  done
  rm -rf ~/tmp/releases/_fixtures-$s
done
```

Rebuilding is not cosmetic. A store built before edge metadata was keyed
`(subject, predicate, object, qualifier_fingerprint)` is **silently unreadable**
by current code — prefix scans match nothing, so `get_edge()` returns `{}` while
node lookups and topology keep working perfectly. Confirm a rebuild with a real
edge read rather than trusting that it finished.

## Local state as of 2026-08-26

| Graph | Where | Format |
| --- | --- | --- |
| `translator_kg_2026-07-19` | `~/tmp/releases/2026-07-19/` (+ ES indices) | 2 |
| `dgidb`, `ttd` | `~/tmp/csrgraph_data/` flat (+ ES indices) | 2 |

All rebuilt from the 2026-07-19 upstream release. Earlier local builds
(`translator_kg` Jun 5, `translator_kg_2026_04`, `processed_tier0_kg`) were
format-1 and have been deleted.

## Keep the Biolink version in sync with the source release

**Every source release pins its own Biolink version, and predicate expansion has
to match it.** The KGX archive records it in `graph-metadata.json` as
`biolinkVersion` (2026-07-19 `translator_kg`: **4.4.2**). Left unpinned,
`BiolinkExpander.from_bmt()` resolves against whatever the toolkit fetches by
default — observed as **4.4.4** — so `treats` expands through a *different* model
than the data was normalised with. That produces wrong answers quietly; there is
no error, just a predicate set that does not match the graph.

This is wired up so it normally takes care of itself:

1. `make_release.py` copies `biolinkVersion` into `manifest.json` as
   `biolink_version`, read during the completeness gate at no extra I/O cost.
   **`--no-gate-completeness` leaves it `null`** — that flag skips the pass that
   reads it.
2. `mcp_server.py` passes the manifest value into `kg_pattern.run(...)`, so
   `graph_query(expand_predicates=True)` expands against the graph's own version.
   `graph_info()` reports it, and `BIOLINK_VERSION` overrides it.
3. `kg_pattern._expander_for()` caches per version, so two versions never share
   one expander.

**So: when you take a new source release, check the version moved.**

```bash
# what the new release declares
curl -s https://kgx-storage.ci.transltr.io/releases/latest-release-summary.json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['translator_kg']['biolink_version'])"

# what the deployed release recorded
python3 -c "import json; print(json.load(open('$HOME/tmp/releases/<version>/manifest.json')).get('biolink_version'))"
```

If they differ, rebuild the release rather than pinning by hand — the manifest is
meant to describe its own data. Releases built before `biolink_version` existed
report `null`; use `BIOLINK_VERSION=4.4.2` until they are rebuilt.

Note the version is a *version*, not a schema location:
`trapi._biolink_schema()` maps `4.4.2` → the tagged
`biolink-model/v4.4.2/biolink-model.yaml` URL, because `bmt.Toolkit(schema=...)`
wants a URL or path and a bare version made it look for a local file called
`4.4.2`. URLs and paths still pass through, so a fork or local checkout works:

```python
trapi.BiolinkExpander.from_bmt(predicates=[...], biolink_version="4.4.2")
```
