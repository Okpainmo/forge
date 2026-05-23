# Forge Intern Onboarding Guide

Welcome to Forge. This guide assumes you are new to the project and maybe new to CI/CD platforms in general. Read it slowly once, then keep it nearby while you work.

## 1. What Forge Is

Forge is two systems working together behind one HTTP API:

1. A CI engine.
2. An artifact registry and dependency resolver.

The CI engine reads a pipeline YAML file, resolves dependencies, prepares a workspace, starts isolated Docker containers, runs the requested build steps, streams logs, and records the final status.

The artifact registry stores packages such as `lib-core@1.0.0`. It verifies checksums, refuses duplicate versions, records metadata in Postgres, and stores artifact bytes under their SHA-256 hash.

Think of Forge as a small internal version of GitHub Actions plus Artifactory.

## 2. Important Words

Artifact: a file produced by a pipeline and published to the registry. Example: `out.tar.gz`.

Coordinate: the package name and version together. Example: `lib-core@1.0.0`.

Pipeline: a YAML file describing dependencies, jobs, steps, and artifacts.

Job: a group of steps that runs in one Docker container.

Step: one shell command inside a job.

Dependency: an artifact that a pipeline needs before it can build.

Lockfile: the exact resolved dependency list, including versions and SHA-256 hashes.

Immutable: once `lib-core@1.0.0` exists, nobody can overwrite it.

SSE: Server-Sent Events. Forge uses this to stream logs live.

## 3. Repository Tour

```text
engine/
  main.py        # FastAPI app and HTTP endpoints
  parser.py      # strict YAML validation
  scheduler.py   # job DAG, run lifecycle, artifact auto-publish
  runner.py      # Docker container execution and dependency pulling
  logs.py        # disk-backed log storage and SSE streaming
  slack.py       # Slack webhook alerts

registry/
  auth.py        # bearer token creation and verification
  config.py      # config.yaml loading
  metadata.py    # Postgres schema and queries
  resolver.py    # custom semver resolver
  storage.py     # artifact upload/download storage logic

cli/
  forge.py       # command-line tool

compose.yaml     # Docker Compose deployment
config.yaml      # runtime settings
requirements.txt # Python dependencies
README.md        # operator documentation
```

## 4. How a Pipeline Run Works

When someone runs:

```bash
forge run pipeline.yaml
```

this happens:

1. The CLI uploads the YAML file to `POST /runs`.
2. The API checks the bearer token.
3. Forge validates the YAML strictly.
4. Forge creates a run row in Postgres with status `queued`.
5. The background run starts and status becomes `running`.
6. The resolver finds exact dependency versions.
7. Forge writes a lockfile into the run metadata.
8. Forge downloads each dependency into `deps/<name>/`.
9. Forge verifies each pulled dependency checksum.
10. The scheduler checks the job DAG for cycles.
11. Ready jobs start in Docker containers.
12. Logs are written to disk and streamed to clients.
13. If all jobs succeed, Forge publishes listed artifacts.
14. The run becomes `succeeded`.

If something goes wrong, the run becomes one of:

```text
failed
integrity_failure
conflict_failure
cycle_failure
```

## 5. First Local Setup

Install Python 3.12 and Docker first.

From the repository root:

```bash
pip install -e .
```

Start the platform:

```bash
docker compose up -d
```

Check containers:

```bash
docker compose ps
```

Create an admin token:

```bash
docker compose exec api python -m registry.auth create-token --name admin
```

Copy the printed token. You will only see it once.

Login with the CLI:

```bash
forge login http://localhost:8080
```

Paste the token when prompted.

## 6. Your First Manual Artifact Upload

Create a tiny artifact:

```bash
mkdir -p /tmp/forge-demo/src
echo "hello" > /tmp/forge-demo/src/hello.txt
tar czf /tmp/lib-core-1.0.0.tar.gz -C /tmp/forge-demo src
```

Publish it:

```bash
forge publish /tmp/lib-core-1.0.0.tar.gz --name lib-core --version 1.0.0
```

List versions:

```bash
forge ls lib-core
```

You should see:

```text
1.0.0
```

Try publishing the same file again. It should fail with `409 Conflict`. That is good. Forge is protecting immutability.

## 7. Your First Pipeline

Create `pipeline.yaml`:

```yaml
name: build-lib-http
version: 1.0.0
dependencies:
  - name: lib-core
    version: "^1.0.0"
jobs:
  build:
    runtime: alpine:3.18
    resources: {cpu: 1.0, memory: 256Mi}
    steps:
      - name: inspect
        run: "ls -R deps"
      - name: package
        run: "mkdir -p src && echo http > src/http.txt && tar czf out.tar.gz src"
artifacts:
  - name: lib-http
    version: 1.0.0
    path: ./out.tar.gz
```

Run it:

```bash
forge run pipeline.yaml
```

The command prints a run ID.

Follow logs:

```bash
forge logs <run-id> --follow
```

Check the published artifact:

```bash
forge ls lib-http
```

## 8. Understanding Pipeline YAML

Required top-level fields:

```yaml
name: string
version: semver
jobs: mapping
artifacts: list
```

Optional top-level field:

```yaml
dependencies: list
```

Each job requires:

```yaml
runtime: docker image
resources:
  cpu: number
  memory: string
steps:
  - name: string
    run: string
```

A job can depend on another job:

```yaml
needs: [test]
```

This means the current job will not run until `test` succeeds.

## 9. Dependency Versions

Forge supports these constraint styles:

Exact:

```text
1.0.0
```

Caret:

```text
^1.0.0
```

This accepts versions from `1.0.0` up to but not including `2.0.0`.

Tilde:

```text
~1.0.0
```

This accepts versions from `1.0.0` up to but not including `1.1.0`.

Comparator range:

```text
>=1.0.0 <2.0.0
```

Forge always chooses the highest version satisfying all constraints.

## 10. Why Checksums Matter

When you publish:

```bash
forge publish file.tar.gz --name lib-core --version 1.0.0
```

the CLI computes SHA-256 and sends it with the upload. The server computes SHA-256 again while receiving the file.

If the hashes differ, the upload is rejected.

When a pipeline later pulls the artifact, Forge computes SHA-256 again and compares it with the lockfile. This protects builds from corrupted or tampered artifacts.

## 11. Logs

Use:

```bash
forge logs <run-id>
```

or:

```bash
forge logs <run-id> --follow
```

Each log line has:

```text
timestamp [job] line
```

Forge stores logs on disk as JSON Lines. It streams old lines first, then live lines. This is why you can connect after a build has already started and still see earlier output.

## 12. Common Failure Modes

400 on upload:

The checksum is wrong, the version is not semver, or the request is malformed.

409 on upload:

That package version already exists. Choose a new version.

conflict_failure:

Dependencies cannot agree on one package version.

cycle_failure:

Either jobs depend on each other in a loop or artifact dependencies form a loop.

integrity_failure:

The downloaded artifact bytes do not match the lockfile hash.

failed:

A build job command exited non-zero, timed out, or hit a runtime failure.

## 13. Safety Rules

Never edit production `config.yaml` casually.

Never reuse package versions. Publish `1.0.1` instead of overwriting `1.0.0`.

Never paste bearer tokens into Slack or screenshots.

Do not mount host directories into job containers manually.

Do not bypass Forge by writing blobs directly into `/var/lib/forge/blobs`.

## 14. Useful Commands

Start services:

```bash
docker compose up -d
```

Stop services:

```bash
docker compose down
```

View API logs:

```bash
docker compose logs -f api
```

Create token:

```bash
docker compose exec api python -m registry.auth create-token --name <your-name>
```

Run pipeline:

```bash
forge run pipeline.yaml
```

Resolve only:

```bash
forge resolve pipeline.yaml
```

Follow logs:

```bash
forge logs <run-id> --follow
```

Publish:

```bash
forge publish out.tar.gz --name package-name --version 1.2.3
```

List versions:

```bash
forge ls package-name
```

## 15. How to Debug a Pipeline

First, resolve dependencies:

```bash
forge resolve pipeline.yaml
```

If that fails, fix dependency versions before trying to run.

Next, run the pipeline:

```bash
forge run pipeline.yaml
```

Then follow logs:

```bash
forge logs <run-id> --follow
```

If a job fails, look for the first command that returned non-zero.

If there are no logs, check API service logs:

```bash
docker compose logs -f api
```

If Docker jobs do not start, check that the API container has the Docker socket mounted:

```bash
docker compose exec api ls -l /var/run/docker.sock
```

## 16. What to Learn Next

Read these files in order:

1. `engine/parser.py`
2. `registry/resolver.py`
3. `registry/storage.py`
4. `engine/scheduler.py`
5. `engine/runner.py`
6. `engine/main.py`

After reading them, you should understand how a YAML file becomes isolated build containers and immutable registry artifacts.
