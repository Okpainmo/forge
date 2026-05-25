# Forge Production Demo

This demo models a common production setup:

- `lib/` is a small shared Python library.
- `api/` is a FastAPI service that depends on the library.
- `pipelines/publish-lib.yaml` publishes the library as a Forge artifact.
- `pipelines/api-ci.yaml` runs API CI after resolving the library from Forge.
- `pipelines/publish-api.yaml` tests and publishes the API release artifact.

The demo keeps runtime work inside Docker:

- `compose.yaml` runs the API locally in a container.
- `Dockerfile.api` builds the local API image.
- `Dockerfile.ci` builds the Docker image used by the Forge API CI job.

## Local API Demo

From this folder:

```bash
docker compose up --build -d
```

Then call the API:

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/quote \
  -H "Content-Type: application/json" \
  -d '{"subtotal":100,"tax_rate":0.075}'
```

Expected quote response:

```json
{ "subtotal": 100.0, "tax_rate": 0.075, "total": "107.50" }
```

Stop the local API:

```bash
docker compose down
```

## Forge Pipeline Demo

Run these commands from the repository root.

First, make sure Forge itself is running:

```bash
docker compose up -d
```

Create and use a Forge token if you have not already logged in:

```bash
docker compose exec api python -m registry.auth create-token --name admin
forge login http://localhost:8080
```

Build the CI runtime image used by the API pipeline:

```bash
docker build -t forge-prod-demo-ci:latest -f prod-demo/Dockerfile.ci prod-demo
```

Publish the shared library artifact:

```bash
forge run prod-demo/pipelines/publish-lib.yaml
```

Follow the returned run ID:

```bash
forge logs <publish-run-id> --follow
```

After this succeeds, Forge stores:

```text
forge-demo-lib@1.0.0
```

Run the API CI pipeline:

```bash
forge run prod-demo/pipelines/api-ci.yaml
```

Follow the returned run ID:

```bash
forge logs <api-ci-run-id> --follow
```

The API CI pipeline declares this dependency:

```yaml
dependencies:
  - name: forge-demo-lib
    version: '1.0.0'
```

Forge resolves that artifact, verifies its checksum, extracts it into:

```text
deps/forge-demo-lib/
```

Then the API tests run with:

```bash
PYTHONPATH=deps/forge-demo-lib:api pytest -q api/tests
```

Publish the API release artifact:

```bash
forge run prod-demo/pipelines/publish-api.yaml
```

Follow the returned run ID:

```bash
forge logs <publish-api-run-id> --follow
```

After this succeeds, Forge stores:

```text
forge-demo-api@0.1.0
```

That completes the production-like chain:

```text
forge-demo-lib@1.0.0
        ↓
API pipeline resolves and verifies the library
        ↓
API tests pass
        ↓
forge-demo-api@0.1.0 is published as an immutable release artifact
```

## Important Current Limitation

Forge currently runs the pipeline YAML, but it does not yet upload or check out the source repository into the job workspace.

Because of that, the demo pipelines recreate the minimal source files inside the job container. The real source still exists in `lib/` and `api/` so you can inspect and run the service locally, but the Forge job cannot read those folders directly yet.

A production-ready next step would be adding source checkout or source upload support so jobs can run directly against the repository contents.

## Re-running the Demo

Artifacts are immutable. After `forge-demo-lib@1.0.0` is published, publishing the same version again returns `409 Conflict`.

To repeat the exact same demo from scratch, reset local Forge state:

```bash
scripts/reset-state.sh --yes
docker compose up -d
docker compose exec api python -m registry.auth create-token --name admin
forge login http://localhost:8080
```

Or change the library artifact version in:

```text
prod-demo/pipelines/publish-lib.yaml
prod-demo/pipelines/api-ci.yaml
prod-demo/pipelines/publish-api.yaml
```

## Can Forge Run CD Workflows Today?

Forge can run shell commands in containers, so technically a pipeline step could deploy something.

In its current state, Forge is better described as CI plus artifact registry, not full CD. It does not yet have deployment environments, approvals, secret management, rollback controls, release promotion, git triggers, or deployment history.

So the honest answer is:

- CI workflows: yes
- artifact publishing: yes
- dependency verification: yes
- production-grade CD workflows: not yet
