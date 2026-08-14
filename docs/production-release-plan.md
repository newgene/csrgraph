# csrgraph production version-release: implementation plan

## Context

csrgraph is code-only; the deployable artifacts (`<graph>.csrgraph.pkl.zst`, the optional
`<graph>.csrgraph.memmap/` dir, and the `<graph>.metadata.lmdb/` store) are built out of band
and carry **no version, hash, or manifest**. The live TRAPI server
(`trapi_server.py`) **loads everything once at startup and holds the LMDB env and memmap
`.bin` files open for the whole process** — there is no hot-reload — so every release ends in
an external stop/start. `LMDBMetadataBackend.build()` does `shutil.rmtree(path)` and rebuilds
**in place**, so a build must never target the live directory. The graph and its LMDB
metadata are keyed together and must be versioned as one immutable unit.

**The store format is coupled to the code, and nothing currently detects a mismatch.**
This is not hypothetical. Edge metadata is keyed
`(subject, predicate, object, qualifier_fingerprint)`; the previous key omitted the
fingerprint. A store built before that change is **silently unreadable** by code after it —
the 4-component prefix scan matches none of the 3-component keys, so `get_edge_variants`
returns `[]`, every qualifier-constrained query answers nothing, and **no error is raised**.
Deploying code and data independently is therefore unsafe today, and a manifest that records
only a data version cannot catch it. See F2.

This plan adds a production release/update story on top of that, with a **shared foundation**
plus **two deployment targets**:

1. **Self-contained** — a single (or few) remote host(s), a separate pull-based updater worker
   doing download → verify → swap → restart. No orchestrator.
2. **Kubernetes** — the cluster is the supervisor; rolling updates replace the worker, and a
   CI/GitOps step (or small controller) bumps the deployed version.

> No code changes yet — this is the plan only.

---

## Shared foundation (both scenarios depend on this)

These pieces are built once and consumed by both deployment models. Three are small csrgraph
code touch-points; the artifact/manifest is the common contract; F5 and F6 are the gates and
constraints that a session of building and rebuilding these stores showed were missing.

**Status: none of F1–F6 is implemented.** The stores this repo runs against were built by
hand, and the hand-work is what the plan encodes.

### F1. Release packaging step (new script: `make_release.py`)
Produce an **immutable release directory** from a KGX archive, reusing existing builders.

**Build into a temporary directory and move it into place only once complete.**
`LMDBMetadataBackend.build()` `rmtree`s its target first, so pointing it at a path that
matters destroys the old copy before it has produced a new one. Rebuilding the 2026-07-19
stores in this repo had to be hand-worked around exactly this way — build to a `.v2` path,
verify, then swap — and F1 exists to make that structural rather than remembered.

Measured costs for the 2026-07-19 graph (28.9M edges), for sizing a release window:

| artifact | time | size |
| --- | --- | --- |
| `.csrgraph.pkl.zst` | ~190 s parse + 1 s save | 34 MB (606 MB in memory) |
| `.csrgraph.memmap/` | 3 s | 745 MB |
| `.metadata.lmdb/` | **3,119 s** | **24 GB** |
| Elasticsearch index (if used) | 1,240 s | 8 GB (3 shards + 1 replica) |

LMDB dominates at ~52 minutes, so a release is roughly an hour of build before any transfer.
- `CSRGraph.from_kgx_archive(...)` then `.save(...)` (csrgraph_kgx.py) for `<graph>.csrgraph.pkl.zst`,
  plus the existing `--build-memmap` path to emit `<graph>.csrgraph.memmap/`.
- `LMDBMetadataBackend.build(...)` (metadata_db.py) for `<graph>.metadata.lmdb/`.
- Write a `manifest.json` (see F2) as the **final** step.

Output layout (the unit that gets shipped/promoted):
```
<version>/
  <graph>.csrgraph.pkl.zst
  <graph>.csrgraph.memmap/         # optional, for instant startup
  <graph>.metadata.lmdb/
  manifest.json
```

### F2. Manifest format (`manifest.json`)
The version/integrity contract the upstream publishes and consumers compare against:
```json
{
  "graph_name": "translator_kg",
  "version": "2026-06-10",
  "built_at": "2026-06-10T12:00:00Z",
  "source_kgx": "translator_kg_2026-06-08.tar.zst",
  "store_format_version": 2,
  "artifacts": {
    "pkl_zst": {"path": "...", "sha256": "...", "bytes": 0},
    "memmap":  {"sha256_tree": "...", "bytes": 0},
    "lmdb":    {"sha256_tree": "...", "bytes": 0}
  },
  "node_count": 0,
  "edge_count": 0,
  "variant_count": 0
}
```

**`store_format_version` is the field that makes independent code/data deploys safe.**
The code declares the format it can read; the server compares at startup and **refuses to
serve** on mismatch rather than answering queries with silently empty results (see the
Context note). Bump it whenever a key layout changes — the qualifier-fingerprint re-key is
version 2.

`variant_count` (distinct `(s, p, o, fingerprint)`) is recorded separately from `edge_count`
(distinct `(s, p, o)`) because they differ — 28,860,305 against 28,105,517 on this graph —
and a release whose variant count collapses to the triple count is one built by pre-version-2
code. That single number catches the regression that cost 754,788 assertions here.
Published to object storage (S3/GCS/HTTPS) as the **last, atomic** step of an upstream build so
no consumer ever sees a half-written manifest. This is the single source both scenarios poll.

### F3. Version/health endpoint (modify `trapi_server.py`)
The server currently has no version awareness, which blocks health-gated swaps and rollback.
- In `_load_graph()` / `_lifespan()`, read `manifest.json` from `DATA_DIR` at startup and stash
  `graph_name` + `version` + `store_format_version` + counts.
- **Fail closed on `store_format_version` mismatch**: log the expected and found versions and
  exit non-zero (scenario 1) / fail readiness (scenario 2). A pod that starts and answers
  everything with zero results is far worse than one that refuses to start.
- Add `GET /version` (and enrich the existing health route) returning that, plus a readiness
  signal once the graph + LMDB are loaded. This endpoint is the gate for both the systemd
  updater (scenario 1) and the k8s readiness probe (scenario 2).
- The existing `/query` route already separates client error (400, invalid query graph) from
  server fault (500), so 5xx alerting is meaningful; `503` remains "graph not loaded".

### F4. LMDB read-only open option (modify `metadata_db.py`)
`LMDBMetadataBackend.__init__` opens the env **read-write** (writes `lock.mdb`). Add an option
(env/arg, e.g. `readonly=True, lock=False`) for the serving path. Needed so releases can live on
read-only mounts / shared RO volumes (scenario 2 option C) and so the serving process never
mutates an immutable release dir. Build path stays read-write.

### F5. Release gates (new — promote nothing that has not passed these)

Two verification tools already exist and both earned their place by catching real regressions.
`make_release.py` should run them against the candidate release and refuse to write
`manifest.json` if either fails, so a bad build cannot become a release.

1. **`probes/verify_variants.py` — store completeness.** Counts distinct `(s, p, o)` and
   `(s, p, o, fingerprint)` in the source edges independently of the store, then compares.
   This is what established that the old key was dropping 754,788 assertions, and what
   confirmed LMDB, Elasticsearch and the source agreed at exactly 28,860,305. It needs the
   extracted `edges.jsonl`, which `make_release.py` already has on hand mid-build — the
   natural place to run it, rather than re-extracting 24 GB later.

2. **`tests/test_corpus.py` — answer invariants.** Data-gated, skipped without a graph, so it
   costs CI nothing and costs a release build one run. It asserts what actually regressed
   historically: no supported query shape returns zero, bindings satisfy their queried
   categories, `query_id` marks exactly the subclass-expanded nodes, and a capped result
   declares itself. Every accuracy regression in this project was caught by the corpus and by
   nothing else — including two introduced while fixing something adjacent.

Deliberately *not* a gate: absolute answer counts. They move whenever the graph is rebuilt,
which would make the gate a tripwire for data changes rather than for defects.

### F6. Elasticsearch deployment constraints (if the ES backend is used)

The release unit above is LMDB-only — an Elasticsearch index lives in a cluster, not in a
directory, so it cannot be shipped as part of an immutable release dir. That has consequences
the plan has to state:

- **The ES index must be reindexed in lockstep with a store-format change.** Document `_id` is
  `subject|predicate|object|fingerprint`; an index built by pre-version-2 code has collapsed
  ids and is missing variants. `store_format_version` covers the LMDB store in the release dir
  but *cannot* cover a cluster the release does not own — so an ES-backed deployment needs its
  index version tracked separately, e.g. in the index name or an index-level metadata doc.
- **Client and server majors must match.** A 9.x Python client cannot talk to an 8.x server at
  all (`Accept version must be either version 8 or 7, but found 9`), and mixing the other way
  fails *partially* — `search` works while `count` returns 404, which produces wrong results
  rather than an outage. Pin the client to the deployed server's major and assert it at startup.
- **`number_of_replicas` must be 0 on a single-node cluster**, or replica shards stay
  unassigned and cluster health goes yellow.
- **Batching is load-bearing over a network.** A remote cluster measured a 28.85 ms pooled
  per-request floor against 0.69 ms for a local one, so 50 sequential point lookups spend
  ~1.4 s in round trips before Elasticsearch does any work. The batched paths absorb this
  (100 triples in one msearch, 92 ms); point-heavy paths do not. An ES backend is only
  reasonable where the access pattern is already batched — see
  `docs/es-backend-vs-remote-2026-08-14.md`.
- **The backend can read an index it did not build.** A production Translator index
  (`processed_tier0_kg_*`) matched this repo's schema field-for-field, prefix-stripping
  included, and served every read operation unmodified. So "point at an existing cluster" is a
  legitimate deployment option, not only "ship our own index".

---

## Scenario 1 — Self-contained host + separate updater worker

Target: one or a handful of VMs/bare-metal hosts, no orchestrator. Capistrano-style release
dirs + atomic `current` symlink + a pull-based updater.

### On-disk layout
```
/srv/csrgraph/
  releases/
    2026-06-10/   2026-06-01/ ...   # immutable, one per F1 release
  current -> releases/2026-06-10     # atomic swap point
```
TRAPI server runs with `DATA_DIR=/srv/csrgraph/current`,
`GRAPH_NAME=translator_kg_2026-07-19`, `NO_ES=1` (LMDB-only per the question's scope). The
graph stem is whatever the release was built as; it is no longer the bare `translator_kg`,
which is now a stale name from the April dataset.

### Components to add
1. **systemd service for the server** — `csrgraph.service` (or templated `csrgraph@.service`
   per color for blue-green) running `.venv/bin/python trapi_server.py` with the env above.
2. **Updater script** (`deploy/updater.py` or `.sh`) — the swap/restart logic, idempotent:
   `flock` (no overlap) → fetch upstream `manifest.json` → compare `version` to deployed
   `current/manifest.json` → if changed: download artifacts to `releases/.<version>.tmp/` →
   verify sha256 against manifest → atomic `mv` into `releases/<version>/` →
   **out-of-band smoke-test** (load the new release in a throwaway process; run a `resolve` +
   1-hop `associations` from `kg_query.py`) → `ln -sfn` flip `current` → restart server →
   poll `GET /version` until it reports the new version (health gate) → on failure
   **auto-rollback** (flip symlink back + restart) → prune to last N releases (only releases
   no running process still holds open).
3. **systemd timer** (`csrgraph-updater.timer`) firing the updater every N minutes. Fail-closed:
   any error leaves `current` untouched.
4. **Privilege**: updater needs `systemctl restart csrgraph` rights (polkit rule, or run server
   as a user unit so the updater can manage it without root).

### Zero-downtime variant (blue-green, optional)
Two server units on two ports (`:8000`/`:8001`) behind nginx. Updater starts the *idle* color on
the new `current`, health-checks it via `/version`, flips the nginx `upstream` and `nginx -s reload`
(graceful drain), then stops the old color. Pairs with the worker so unattended swaps drop no
requests. Otherwise restart-in-place gives downtime = load time (near-instant with memmap).

### Why the swap is safe under load
A symlink flip doesn't disturb the running process's already-open LMDB/memmap inodes (Unix keeps
the old inode alive until the old process exits). The new release lives in a different dir, so
prepare is fully non-disruptive; only the restart/flip is the cutover.

---

## Scenario 2 — Kubernetes production deployment

Target: cluster-managed. **Kubernetes is the supervisor** — its rolling update replaces the
custom worker's swap/restart/rollback entirely. The "monitor upstream" role moves to CI/GitOps.

### How the artifact reaches pods — recommended: init-container pull
- An **init container** reads an artifact `version` (env/arg, sourced from the manifest) and
  downloads that release from object storage into a per-pod ephemeral volume (`emptyDir`); the
  main TRAPI container points `DATA_DIR` at it and mmaps/loads as today. Keeps images small;
  version is a plain env value; immutable per pod.
- **Alternative A (bake into image)** — simplest for smaller graphs: `COPY <version>/` into the
  image, image tag = version, rollback = redeploy old tag. Downside: multi-GB images.
- **Alternative C (shared RO PVC)** — `ReadOnlyMany` PVC (NFS/EFS) holding `releases/`; a Job
  builds new releases onto it; Deployment env points at a versioned subpath. Closest to
  scenario-1's model, shared across pods, best for very large artifacts. **Requires F4** (LMDB
  read-only open) because the PVC is mounted read-only and LMDB otherwise tries to write
  `lock.mdb`. Concurrent read-only LMDB access across pods is fine.

### Deployment mechanics
- **Deployment** (or Helm chart) with `RollingUpdate` strategy; env `DATA_DIR`, `GRAPH_NAME`,
  `NO_ES=1` via ConfigMap.
- **Probes**: readiness + liveness hit `GET /version` (F3). Readiness gates traffic until the
  graph + LMDB are loaded, so rolling update naturally drains old pods only after new ones serve.
- **Release = version bump**: changing the image tag (Alt A) or the `version` env / subpath
  (init-container or PVC) triggers a rolling update; the old ReplicaSet is torn down after the
  new pods pass readiness. **Rollback = `kubectl rollout undo`.**
- **The "upstream monitor"** becomes a CI/CD pipeline or a small CronJob/controller (or Argo
  CD / Flux) that watches the F2 manifest and bumps the image tag / version env (e.g. via
  `kubectl set image` or a GitOps commit). Same manifest contract as scenario 1.

### Why no custom worker here
Smoke-test gate → readiness probe; atomic swap → rolling update; health-gated auto-rollback →
`rollout undo` on failed readiness; prune old → ReplicaSet/image GC. Re-implementing those in a
sidecar would duplicate what the platform already guarantees.

---

## When to use which
- **Scenario 1** for one/few hosts, no cluster, full control, minimal moving parts (systemd timer
  + script). The worker *is* the value-add because nothing else supervises the restart.
- **Scenario 2** when already on k8s — let the platform own swap/restart/rollback; only build the
  manifest contract + version-bump automation. The shared foundation (F1–F4) is identical; only
  the delivery/supervision layer differs.

---

## Files to add / modify

**Shared (csrgraph code):**
- `trapi_server.py` — F3: read `manifest.json` at startup; add/enrich `/version` + readiness;
  **fail closed on `store_format_version` mismatch**.
- `metadata_db.py` — F4: `readonly`/`lock` option on `LMDBMetadataBackend.__init__`; declare the
  format version the code reads (the constant F3 compares against).
- `make_release.py` (new) — F1 packaging into a temp dir then atomic move, reusing
  `from_kgx_archive`/`save`/`--build-memmap` and `LMDBMetadataBackend.build`; runs the F5 gates;
  emits `manifest.json` (F2) as the last step.

**Scenario 1 (ops, no app code):**
- `deploy/updater.py` (or `.sh`) + `deploy/csrgraph.service` + `deploy/csrgraph-updater.timer`
  (+ optional `deploy/nginx.conf` and a second `csrgraph@.service` for blue-green).

**Scenario 2 (ops, no app code):**
- `Dockerfile`, `deploy/k8s/` manifests (Deployment + Service + ConfigMap, init-container script)
  or a small Helm chart; optional CronJob/GitOps config for version bumps.

---

## Verification
- **Foundation**: build a small sample release (`dgidb`/`ttd`) with `make_release.py`; confirm
  `manifest.json` sha256/counts match; start `trapi_server.py` against it and check `GET /version`
  reports the manifest version; confirm LMDB opens with the new read-only flag.
- **Store-format guard** (the one that protects against silent emptiness): hand-edit
  `store_format_version` in a good release's manifest and confirm the server refuses to start
  rather than serving zero-result queries. `dgidb` is large enough to test this in seconds and
  small enough to rebuild, and it already exercises qualifier variants — 52,065 records over
  51,943 distinct triples, 116 of them multi-variant.
- **Release gates**: corrupt a store (drop a variant) and confirm `make_release.py` fails at
  F5 and writes no `manifest.json`, so the release never exists rather than existing broken.
- **Scenario 1**: stand up `releases/` + `current` locally; run the updater against a fake
  upstream manifest pointing at a second sample release; verify it downloads, smoke-tests, flips,
  restarts, and `/version` flips — then corrupt a release and confirm it fails closed +
  auto-rolls-back. Verify `flock` prevents overlapping runs.
- **Scenario 2**: build the image (or init-container pull) in a local cluster (kind/minikube);
  apply the Deployment; confirm readiness gates on `/version`; bump the version and watch the
  rolling update cut over with no failed requests; `kubectl rollout undo` to confirm rollback.
- Existing `.venv/bin/python -m pytest -q` must stay green (foundation changes are additive).
