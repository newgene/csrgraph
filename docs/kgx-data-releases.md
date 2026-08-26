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

## One version to watch

The 2026-07-19 `translator_kg` reports `biolink_version: 4.4.2`, but
`BiolinkExpander.from_bmt()` with no `biolink_version` resolves against whatever
the toolkit fetches by default — observed as **4.4.4**. Predicate expansion then
uses a slightly different model than the data was normalised with. Pin it to the
graph's version when that matters:

```python
trapi.BiolinkExpander.from_bmt(predicates=[...], biolink_version="4.4.2")
```
