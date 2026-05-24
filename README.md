# Forge

Forge is a self-hosted CI/CD and artifact infrastructure platform that combines isolated pipeline execution, deterministic dependency resolution, real-time build orchestration, and an immutable artifact registry into a single production-grade system. Built for trustable software delivery, Forge executes YAML-defined pipelines inside hardened sandboxed environments, resolves and verifies dependencies with checksum integrity guarantees, streams logs live over SSE, and publishes versioned artifacts through a content-addressable registry with strict immutability and semver-aware resolution.

Designed as a lightweight but deeply engineered alternative to platforms like GitHub Actions, JFrog, and Sonatype, Forge focuses on core platform engineering fundamentals: scheduler design, isolation, integrity verification, dependency graph resolution, and reproducible builds — all deployable on a single VPS using Docker Compose.

Public URL: set `server.public_url` in `config.yaml` to the live VPS URL.

## Required API

All write operations require `Authorization: Bearer <token>`.

```text
POST /runs                              multipart {pipeline: <file>}            -> {run_id}
GET  /runs/{id}                                                                  -> {status, jobs, lockfile_url}
GET  /runs/{id}/lockfile                                                         -> {lockfile JSON}
GET  /runs/{id}/logs?follow=true        SSE stream                               -> {ts, job, line}
POST /artifacts/{name}/{version}        multipart {file, checksum: sha256:hex}   -> 201 / 400 / 409
GET  /artifacts/{name}/{version}                                                 -> blob, X-Artifact-SHA256
GET  /artifacts/{name}/{version}/meta                                            -> metadata JSON
GET  /artifacts/{name}                                                           -> {versions: [...]}
```

Forge also provides `POST /resolve` for `forge resolve`. The required API remains present exactly as specified.

## Pipeline YAML

Annotated example:

```yaml
name: build-lib-http              # Required pipeline name
version: 1.0.0                    # Required semver pipeline version

dependencies:                    # Optional direct registry dependencies
  - name: lib-core
    version: "^1.0.0"             # exact, ^, ~, or comparator range

jobs:
  build:
    runtime: alpine:3.18          # Docker image for this job
    resources:
      cpu: 1.0                    # enforced through Docker nano_cpus
      memory: 512Mi               # enforced through Docker mem_limit
    steps:
      - name: test
        run: "sh ./test.sh"
      - name: package
        run: "tar czf out.tar.gz src/"

artifacts:
  - name: lib-http
    version: 1.0.0
    path: ./out.tar.gz            # published after all jobs succeed
```

Validation is strict. Unknown fields fail. Missing required fields fail. Errors include a YAML line number.

Jobs may declare dependencies:

```yaml
jobs:
  test:
    runtime: alpine:3.18
    resources: {cpu: 1.0, memory: 256Mi}
    steps:
      - {name: test, run: "echo ok"}
  package:
    needs: [test]
    runtime: alpine:3.18
    resources: {cpu: 1.0, memory: 256Mi}
    steps:
      - {name: package, run: "tar czf out.tar.gz ."}
```

## Scheduler

The scheduler is implemented in `engine/scheduler.py`; it does not use a workflow engine. Before any job runs, Forge builds a directed graph from `jobs.<name>.needs`, checks that all referenced jobs exist, and detects cycles with depth-first traversal. Independent jobs become runnable when all of their dependencies have succeeded.

The concurrency limit comes from `runner.concurrency` in `config.yaml`. If a job fails, its dependents are marked `skipped`, not `failed`. The run itself becomes `failed` and records the failing job.

## Isolation

Forge uses Docker containers for job isolation, implemented in `engine/runner.py`.

Each job gets:

- Its own container.
- A mounted run workspace at `/workspace`.
- Docker PID, mount, and network namespaces.
- CPU limits via `nano_cpus`.
- Memory limits via `mem_limit`.
- Process count limits via `pids_limit`.
- Dropped Linux capabilities.
- `no-new-privileges`.
- A wall-clock timeout.

Build containers are attached only to the internal Docker network configured as `forge_internal`. The Compose network is marked `internal: true`, so job containers can reach the API/registry service but not the public internet through that network. This is the key control for non-registry network egress.

The API container mounts `/var/run/docker.sock` so it can start job containers. This is acceptable for the assignment and common in small runners, but it means the API service must be treated as trusted infrastructure.

## Storage Layer

Artifact bytes are content-addressed by SHA-256:

```text
/var/lib/forge/blobs/<first-two-hash-chars>/<sha256>
```

Postgres stores metadata:

- `name`
- `version`
- `sha256`
- `size`
- `publisher`
- `published_at`
- declared dependency metadata

The `(name, version)` pair has a unique database constraint. If two clients race to publish the same version, exactly one transaction can insert the row. The loser receives `409 Conflict`, preserving immutability.

The server computes SHA-256 while streaming uploads to disk. If the computed checksum does not match the client-declared `sha256:<hex>`, Forge deletes the temp file and returns `400`.

## Resolver

The dependency resolver is implemented from scratch in `registry/resolver.py`. It supports:

- exact versions: `1.0.0`
- caret ranges: `^1.0.0`
- tilde ranges: `~1.0.0`
- comparator ranges: `>=1.0.0 <2.0.0`

Resolution walks transitive metadata from the registry, accumulates all constraints for each package, chooses the highest version satisfying every known constraint, and repeats until the selected set is stable. Cycles are reported with the cycle path. Conflicts report the package plus the dependency paths and constraints that could not be satisfied.

The lockfile is deterministic because package names and dependencies are sorted before traversal and output. The same pipeline and same registry state produce byte-identical JSON when serialized with sorted keys.

## Pull-Time Integrity

Before any build step runs, Forge downloads every locked dependency into:

```text
/workspace/deps/<name>/
```

It recomputes SHA-256 from the received bytes and compares the result to the lockfile. If the values differ, the run becomes `integrity_failure`, both hashes are logged, and a Slack integrity alert is sent.

## Log Streaming

Logs are JSON Lines files under `/var/lib/forge/logs`. Each event contains:

```json
{"ts": "...", "job": "build", "line": "..."}
```

The runner writes each line to disk at the time it is observed and broadcasts it to active SSE subscribers. A client connecting mid-build first receives the backlog by streaming the log file from disk, then receives new live events. Forge does not load an entire 50MB log into memory.

## Slack Alerts

Configure `slack.webhook_url` and tags in `config.yaml`.

Events sent:

- pipeline started
- pipeline succeeded
- pipeline failed
- integrity failure
- resolution failure

Screenshot placeholder: add the captured Slack alert image to your submission package after testing the deployed webhook.

## Fresh VPS Setup

1. Install Docker and Docker Compose on the VPS.

2. Clone the project:

   ```bash
   git clone <your-repo-url> forge
   cd forge
   ```

3. Edit `config.yaml`:

   ```yaml
   server:
     public_url: http://YOUR_STATIC_IP_OR_DOMAIN:8080
   slack:
     webhook_url: https://hooks.slack.com/services/...
   ```

4. Start the platform:

   ```bash
   docker compose up -d
   ```

5. Create the first token:

   ```bash
   docker compose exec api python -m registry.auth create-token --name admin
   ```

6. Install the CLI locally or on the VPS:

   ```bash
   pip install -e .
   ```

7. Log in:

   ```bash
   forge login http://YOUR_STATIC_IP_OR_DOMAIN:8080
   ```

8. Publish or run pipelines:

   ```bash
   forge run pipeline.yaml
   forge logs <run-id> --follow
   forge ls lib-core
   ```

## Required Scenario Checks

Use these checks before submission:

- `lib-core@1.0.0` pipeline builds and publishes.
- `lib-http@1.0.0` pipeline resolves `lib-core@^1.0.0` and publishes.
- `service-api@0.1.0` resolves both and publishes.
- Wrong checksum upload returns `400`.
- Duplicate upload returns `409`.
- Version conflict fails before any job starts.
- Filesystem escape, memory exhaustion, and non-registry egress are contained by the container.
- A pipeline emitting about 50MB logs remains streamable with `forge logs --follow`.
