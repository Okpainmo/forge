from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.comments import CommentedMap

from registry.resolver import parse_constraint
from registry.storage import NAME_RE, SEMVER_RE


class PipelineValidationError(Exception):
    pass


def line_of(node: Any, key: str | None = None) -> int:
    try:
        if key is not None and isinstance(node, CommentedMap):
            return node.lc.key(key)[0] + 1
        return node.lc.line + 1
    except Exception:
        return 1


def fail(node: Any, message: str, key: str | None = None) -> None:
    raise PipelineValidationError(f"line {line_of(node, key)}: {message}")


def require_map(node: Any, where: str) -> CommentedMap:
    if not isinstance(node, CommentedMap):
        fail(node, f"{where} must be a mapping")
    return node


def check_keys(node: CommentedMap, allowed: set[str], required: set[str], where: str) -> None:
    for key in node:
        if key not in allowed:
            fail(node, f"unknown field '{key}' in {where}", key)
    for key in required:
        if key not in node:
            fail(node, f"missing required field '{key}' in {where}")


def plain(value: Any) -> Any:
    if isinstance(value, CommentedMap):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def validate_pipeline_text(text: str) -> dict[str, Any]:
    yaml = YAML(typ="rt")
    try:
        root = yaml.load(text)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        line = mark.line + 1 if mark else 1
        raise PipelineValidationError(f"line {line}: invalid YAML: {exc.problem or exc}") from exc
    root = require_map(root, "pipeline")
    check_keys(root, {"name", "version", "dependencies", "jobs", "artifacts"}, {"name", "version", "jobs", "artifacts"}, "pipeline")
    if not isinstance(root["name"], str):
        fail(root, "name must be a string", "name")
    if not isinstance(root["version"], str) or not SEMVER_RE.match(root["version"]):
        fail(root, "version must be semver MAJOR.MINOR.PATCH", "version")

    deps = root.get("dependencies") or []
    if not isinstance(deps, list):
        fail(root, "dependencies must be a list", "dependencies")
    for dep in deps:
        dep = require_map(dep, "dependency")
        check_keys(dep, {"name", "version"}, {"name", "version"}, "dependency")
        if not isinstance(dep["name"], str) or not NAME_RE.match(dep["name"]):
            fail(dep, "dependency name must use letters, numbers, dot, underscore, or dash", "name")
        try:
            parse_constraint(str(dep["version"]))
        except ValueError as exc:
            fail(dep, str(exc), "version")

    jobs = require_map(root["jobs"], "jobs")
    for job_name, job in jobs.items():
        if not isinstance(job_name, str) or not re.match(r"^[a-zA-Z0-9._-]+$", job_name):
            fail(jobs, "job name must be a string", job_name)
        job = require_map(job, f"job {job_name}")
        check_keys(job, {"runtime", "resources", "steps", "needs", "timeout_seconds"}, {"runtime", "resources", "steps"}, f"job {job_name}")
        if not isinstance(job["runtime"], str):
            fail(job, "runtime must be a string", "runtime")
        resources = require_map(job["resources"], f"job {job_name} resources")
        check_keys(resources, {"cpu", "memory"}, {"cpu", "memory"}, f"job {job_name} resources")
        if not isinstance(resources["cpu"], (int, float)):
            fail(resources, "cpu must be a number", "cpu")
        if not isinstance(resources["memory"], str):
            fail(resources, "memory must be a string like 512Mi", "memory")
        needs = job.get("needs", [])
        if needs is None:
            needs = []
        if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
            fail(job, "needs must be a list of job names", "needs")
        steps = job["steps"]
        if not isinstance(steps, list) or not steps:
            fail(job, "steps must be a non-empty list", "steps")
        for step in steps:
            step = require_map(step, f"job {job_name} step")
            check_keys(step, {"name", "run"}, {"name", "run"}, f"job {job_name} step")
            if not isinstance(step["name"], str) or not isinstance(step["run"], str):
                fail(step, "step name and run must be strings")

    artifacts = root["artifacts"]
    if not isinstance(artifacts, list):
        fail(root, "artifacts must be a list", "artifacts")
    for artifact in artifacts:
        artifact = require_map(artifact, "artifact")
        check_keys(artifact, {"name", "version", "path"}, {"name", "version", "path"}, "artifact")
        if not isinstance(artifact["name"], str) or not NAME_RE.match(artifact["name"]):
            fail(artifact, "artifact name must use letters, numbers, dot, underscore, or dash", "name")
        if not isinstance(artifact["version"], str) or not SEMVER_RE.match(artifact["version"]):
            fail(artifact, "artifact version must be semver", "version")
        if not isinstance(artifact["path"], str):
            fail(artifact, "artifact path must be a string", "path")

    return plain(root)


def validate_pipeline_file(path: Path) -> dict[str, Any]:
    return validate_pipeline_text(path.read_text(encoding="utf-8"))
