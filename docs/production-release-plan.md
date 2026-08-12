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

This plan adds a production release/update story on top of that, with a **shared foundation**
plus **two deployment targets**:

1. **Self-contained** — a single (or few) remote host(s), a separate pull-based updater worker
   doing download → verify → swap → restart. No orchestrator.
2. **Kubernetes** — the cluster is the supervisor; rolling updates replace the worker, and a
   CI/GitOps step (or small controller) bumps the deployed version.

> No code changes yet — this is the plan only.

---

## Shared foundation (both scenarios depend on this)

These four pieces are built once and consumed by both deployment models. Three are small
csrgraph code touch-points; the artifact/manifest is the common contract.

### F1. Release packaging step (new script: `make_release.py`)
Produce an **immutable release directory** from a KGX archive, reusing existing builders:
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
  "artifacts": {
    "pkl_zst": {"path": "...", "sha256": "...", "bytes": 0},
    "memmap":  {"sha256_tree": "...", "bytes": 0},
    "lmdb":    {"sha256_tree": "...", "bytes": 0}
  },
  "node_count": 0,
  "edge_count": 0
}
```
Published to object storage (S3/GCS/HTTPS) as the **last, atomic** step of an upstream build so
no consumer ever sees a half-written manifest. This is the single source both scenarios poll.

### F3. Version/health endpoint (modify `trapi_server.py`)
The server currently has no version awareness, which blocks health-gated swaps and rollback.
- In `_load_graph()` / `_lifespan()`, read `manifest.json` from `DATA_DIR` at startup and stash
  `graph_name` + `version` + counts.
- Add `GET /version` (and enrich the existing health route) returning that, plus a readiness
  signal once the graph + LMDB are loaded. This endpoint is the gate for both the systemd
  updater (scenario 1) and the k8s readiness probe (scenario 2).

### F4. LMDB read-only open option (modify `metadata_db.py`)
`LMDBMetadataBackend.__init__` opens the env **read-write** (writes `lock.mdb`). Add an option
(env/arg, e.g. `readonly=True, lock=False`) for the serving path. Needed so releases can live on
read-only mounts / shared RO volumes (scenario 2 option C) and so the serving process never
mutates an immutable release dir. Build path stays read-write.

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
TRAPI server runs with `DATA_DIR=/srv/csrgraph/current`, `GRAPH_NAME=translator_kg`, `NO_ES=1`
(LMDB-only per the question's scope).

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
- `trapi_server.py` — F3: read `manifest.json` at startup; add/enrich `/version` + readiness.
- `metadata_db.py` — F4: `readonly`/`lock` option on `LMDBMetadataBackend.__init__`.
- `make_release.py` (new) — F1 packaging, reusing `from_kgx_archive`/`save`/`--build-memmap` and
  `LMDBMetadataBackend.build`; emits `manifest.json` (F2).

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
- **Scenario 1**: stand up `releases/` + `current` locally; run the updater against a fake
  upstream manifest pointing at a second sample release; verify it downloads, smoke-tests, flips,
  restarts, and `/version` flips — then corrupt a release and confirm it fails closed +
  auto-rolls-back. Verify `flock` prevents overlapping runs.
- **Scenario 2**: build the image (or init-container pull) in a local cluster (kind/minikube);
  apply the Deployment; confirm readiness gates on `/version`; bump the version and watch the
  rolling update cut over with no failed requests; `kubectl rollout undo` to confirm rollback.
- Existing `.venv/bin/python -m pytest -q` must stay green (foundation changes are additive).
