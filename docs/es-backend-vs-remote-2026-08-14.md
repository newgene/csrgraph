# LMDB vs local ES vs a remote production ES — 2026-08-14

Re-running the metadata-backend comparison against a **real** Elasticsearch
deployment (`biothings_es8`, reached on a forwarded `localhost:9212`) alongside
the local podman cluster, to see whether the local test cluster was flattering
Elasticsearch.

**Nothing was written to the production cluster.** Every operation was a read,
against indices that already existed. No index was created, modified or deleted.

## The two clusters are not comparable hardware

| | local test cluster | production |
| --- | --- | --- |
| cluster | `csrgraph-cluster` | `biothings_es8` |
| version | 9.5.0 | **8.17.0** |
| nodes | 3 (podman, loopback) | 1 data node |
| indices | 2 (ours only) | **665** |
| active shards | 16 | **1,199** |
| reached via | loopback | forwarded port |

## Two setup traps, both of which produced garbage first

**1. The 9.x client cannot talk to an 8.x server.** It fails outright:

```
BadRequestError(400, 'media_type_header_exception',
  'Accept version must be either version 8 or 7, but found 9')
```

Each server has to be driven by a client of its own major, so the production
figures were taken with an `elasticsearch>=8,<9` client in a separate venv.
Mixing does not merely warn — an 8.x client against the 9.5 server has `search`
work while `count` returns `404`, so results come back *partially* wrong.

**2. A forwarded port shadowed a local cluster node.** Port 9201 stopped being
`es02` and started being an AWS load balancer:

| port | actually listening |
| --- | --- |
| 9200 | local `csrgraph-cluster` 9.5.0 |
| **9201** | **`Server: awselb/2.0`** — not ours |
| 9202 | local `csrgraph-cluster` 9.5.0 |
| 9212 | production `biothings_es8` 8.17.0 |

The client round-robins, so a third of every request went over the network to a
load balancer that answered `404`. Local `get_node x50` measured **1,773 ms**
that way against **54 ms** once 9201 was dropped — a **33× error**, and the kind
that looks like a plausible result rather than a failure. Multi-host clients need
their host list checked, not assumed.

## Results

Identical fixtures throughout: 100 node CURIEs and 100 triples verified present
in **both** datasets (the production snapshot carries 28,540,165 edges against
our 28,860,305, so it is a different build of the same KG). Median of 5 after a
warm-up.

| operation | LMDB | ES local | ES prod | prod ÷ local |
| --- | ---: | ---: | ---: | ---: |
| `get_node` ×50 | **0.28 ms** | 54.4 ms | 1,777 ms | 33× |
| `get_edge` ×50 | **0.35 ms** | 44.3 ms | 1,834 ms | 41× |
| `get_edge_variants` ×50 | **0.34 ms** | 112.1 ms | 1,914 ms | 17× |
| `filter_nodes` (100, batched) | **0.53 ms** | 3.2 ms | 92.0 ms | 29× |
| `filter_edges` (100, batched) | **0.60 ms** | 16.5 ms | 91.8 ms | 6× |
| `nodes_by_category` | **0.13 ms** | 45.5 ms | 350.9 ms | 8× |

## Most of that gap is the wire, not Elasticsearch

Pooled per-request floor, measured with each client's own connection pool:

| | median | min | p90 |
| --- | ---: | ---: | ---: |
| local | **0.69 ms** | 0.46 | 0.94 |
| production | **28.85 ms** | 20.92 | 34.30 |

A sequential 50-point-lookup therefore pays ~1.44 s of pure round-trip before
Elasticsearch does anything. Subtracting it, production's server-side cost is
roughly 7–9 ms per point op against local's ~1 ms — slower, which is what a
single node carrying 1,199 shards under other tenants' load should look like, but
nothing like the 33× the raw numbers suggest.

The batched operations are where this matters. `filter_edges` over 100 triples is
**one** msearch: 92 ms remote, of which ~29 ms is the round trip. The same work
as 100 point lookups would have cost ~2.9 s in round trips alone. The batching in
`match_path`'s `_flush` is not a micro-optimisation against a remote backend —
it is the difference between viable and unusable.

## What this changes

**Nothing about the standing recommendation.** LMDB remains 1–3 orders of
magnitude faster on every metadata operation, and being in-process it has no
round trip to amortise at all. The local test cluster was *not* flattering
Elasticsearch: on server-side work it is only ~7× better than a loaded
production node, and the rest of the difference is network distance that any real
deployment pays.

Elasticsearch keeps the roles it already had — full-text resolution,
aggregations, and horizontal scale — with one sharpened caveat: **per-request
latency dominates unless calls are batched**, so a remote ES backend is only
reasonable where the access pattern is already batched.

## Incidental: our schema is portable

`processed_tier0_kg_nodes` / `_edges` turned out to match this repo's
Elasticsearch schema exactly — `subject`/`predicate`/`object`/`knowledge_level`/
`agent_type` as `keyword` on edges, `id`/`category` as `keyword` plus `name` as
`text` on nodes, and both with the `biolink:` prefix stripped, which is what
`_strip_biolink` / `_add_biolink` already do. `ElasticsearchMetadataBackend`
served `get_node`, `get_edge`, `get_edge_variants`, `filter_nodes`,
`filter_edges` and `nodes_by_category` off those indices unmodified, and derived
the right keyword-only pushdown set (`name` correctly excluded as analysed text).

So the backend can read a Translator ES index it did not build. That is worth
knowing for deployment: the metadata store need not be ours.

## Addendum: re-verified on the full 3-node cluster

Port 9201 was restored to `es02`, so the local figures above — taken on two of
three nodes while an AWS load balancer occupied 9201 — were re-measured against
the complete cluster.

**Node count made no measurable difference.** Three repeats of each configuration,
same fixtures:

| operation | 2-node range | 3-node range |
| --- | --- | --- |
| `get_node` ×50 | 49.8 – 59.2 ms | 51.7 – 64.3 ms |
| `get_edge` ×50 | 40.8 – 48.7 ms | 43.0 – 51.6 ms |
| `get_edge_variants` ×50 | 98.2 – 118.3 ms | 104.1 – 119.7 ms |
| `filter_nodes` (100) | 3.32 – 3.69 ms | 3.34 – 3.68 ms |
| `filter_edges` (100) | 12.5 – 20.9 ms | 13.5 – 15.6 ms |
| `nodes_by_category` | 44.3 – 44.7 ms | 44.9 – 45.8 ms |

Every range overlaps. The first 3-node sample read 68.9 ms on `get_node`, the
highest of seven runs, and settled once `es02` warmed — a freshly restarted node
is cold, not slow. The numbers in the table above sit inside these ranges and
stand as recorded.

Two things follow. Adding a third coordinating node does **not** speed these
operations up: a point lookup is routed to whichever node holds the shard
regardless of which one receives it, so more entry points buy nothing on this
workload — they matter for concurrency, which is measured separately in
`docs/concurrency-and-scalability-2026-07-19.md`.

And run-to-run variance on the point operations is roughly **±20%**, so the
`prod ÷ local` ratios should be read as order-of-magnitude, not to two
significant figures. The conclusion is unaffected: it rests on the pooled
per-request floor (0.69 ms against 28.85 ms), which is a far larger and much more
stable difference than this noise.
