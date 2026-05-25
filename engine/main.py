from __future__ import annotations

import json
import uuid
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from engine.logs import stream
from engine.parser import PipelineValidationError, validate_pipeline_text
from engine.scheduler import execute_run
from registry import metadata
from registry.auth import require_identity
from registry.resolver import DependencyCycleError, ResolutionError, parse_constraint, resolve
from registry.storage import ArtifactError, NAME_RE, blob_path, store_upload

app = FastAPI(title="Forge CI/CD and Artifact Registry")


@app.on_event("startup")
def startup() -> None:
    metadata.init_db()


def auth(authorization: str | None) -> str:
    try:
        return require_identity(authorization)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def validate_deps_payload(deps: object) -> list[dict]:
    if not isinstance(deps, list):
        raise HTTPException(status_code=400, detail="deps must be a JSON list")
    for dep in deps:
        if not isinstance(dep, dict) or set(dep) != {"name", "version"}:
            raise HTTPException(status_code=400, detail="each dependency must contain name and version")
        if not isinstance(dep["name"], str) or not NAME_RE.match(dep["name"]):
            raise HTTPException(status_code=400, detail="invalid dependency name")
        if not isinstance(dep["version"], str):
            raise HTTPException(status_code=400, detail="dependency version must be a string")
        try:
            parse_constraint(dep["version"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return deps


@app.get("/")
def welcome() -> dict[str, str]:
    return {
        "name": "Forge",
        "message": "Forge CI/CD and Artifact Registry API is running.",
        "docs_url": "/docs",
        "health_url": "/health",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/runs")
async def create_run(
    background: BackgroundTasks,
    pipeline: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    publisher = auth(authorization)
    text = (await pipeline.read()).decode("utf-8")
    try:
        parsed = validate_pipeline_text(text)
    except PipelineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run_id = str(uuid.uuid4())
    metadata.create_run(run_id, parsed["name"], "queued")
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    background.add_task(execute_run, run_id, parsed, token, publisher)
    return {"run_id": run_id}


@app.post("/resolve")
async def resolve_pipeline(
    pipeline: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict:
    auth(authorization)
    text = (await pipeline.read()).decode("utf-8")
    try:
        parsed = validate_pipeline_text(text)
        return resolve(parsed.get("dependencies") or [])
    except PipelineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DependencyCycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/whoami")
def whoami(authorization: str | None = Header(default=None)) -> dict[str, str]:
    return {"name": auth(authorization)}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    row = metadata.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "status": row["status"],
        "jobs": row["jobs"],
        "lockfile_url": f"/runs/{run_id}/lockfile" if row.get("lockfile") else None,
        "failure_reason": row.get("failure_reason"),
    }


@app.get("/runs/{run_id}/lockfile")
def get_lockfile(run_id: str) -> dict:
    row = metadata.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    if row.get("lockfile") is None:
        raise HTTPException(status_code=404, detail="lockfile not available")
    return row["lockfile"] if isinstance(row["lockfile"], dict) else json.loads(row["lockfile"])


@app.get("/runs/{run_id}/logs")
async def get_logs(run_id: str, follow: bool = False) -> StreamingResponse:
    return StreamingResponse(stream(run_id, follow), media_type="text/event-stream")


@app.post("/artifacts/{name}/{version}")
async def post_artifact(
    name: str,
    version: str,
    file: UploadFile = File(...),
    checksum: str = Form(...),
    deps: str = Form(default="[]"),
    authorization: str | None = Header(default=None),
):
    publisher = auth(authorization)
    try:
        parsed_deps = validate_deps_payload(json.loads(deps))
        row = store_upload(
            name=name,
            version=version,
            checksum=checksum,
            fileobj=file.file,
            publisher=publisher,
            deps=parsed_deps,
        )
        return JSONResponse(row, status_code=201)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="deps must be JSON") from exc
    except ArtifactError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@app.get("/artifacts/{name}/{version}")
def download_artifact(name: str, version: str):
    meta = metadata.get_artifact(name, version)
    if not meta:
        raise HTTPException(status_code=404, detail="artifact not found")
    path = blob_path(meta["sha256"])
    return FileResponse(path, media_type="application/octet-stream", headers={"X-Artifact-SHA256": meta["sha256"]})


@app.get("/artifacts/{name}/{version}/meta")
def artifact_meta(name: str, version: str) -> dict:
    meta = metadata.get_artifact(name, version)
    if not meta:
        raise HTTPException(status_code=404, detail="artifact not found")
    return meta


@app.get("/artifacts/{name}")
def artifact_versions(name: str) -> dict[str, list[str]]:
    return {"versions": metadata.list_versions(name)}
