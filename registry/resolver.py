from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable

from registry.metadata import get_artifact, list_artifacts

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class ResolutionError(Exception):
    status = "conflict_failure"


class DependencyCycleError(ResolutionError):
    status = "cycle_failure"


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, raw: str) -> "Version":
        match = SEMVER_RE.match(raw)
        if not match:
            raise ValueError(f"invalid semver version: {raw}")
        return cls(*(int(p) for p in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class Comparator:
    op: str
    version: Version

    def matches(self, version: Version) -> bool:
        if self.op == "==":
            return version == self.version
        if self.op == ">=":
            return version >= self.version
        if self.op == ">":
            return version > self.version
        if self.op == "<=":
            return version <= self.version
        if self.op == "<":
            return version < self.version
        raise ValueError(self.op)


def parse_constraint(raw: str) -> list[Comparator]:
    raw = str(raw).strip()
    if raw.startswith("^"):
        base = Version.parse(raw[1:])
        if base.major > 0:
            upper = Version(base.major + 1, 0, 0)
        elif base.minor > 0:
            upper = Version(0, base.minor + 1, 0)
        else:
            upper = Version(0, 0, base.patch + 1)
        return [Comparator(">=", base), Comparator("<", upper)]
    if raw.startswith("~"):
        base = Version.parse(raw[1:])
        return [Comparator(">=", base), Comparator("<", Version(base.major, base.minor + 1, 0))]
    if SEMVER_RE.match(raw):
        return [Comparator("==", Version.parse(raw))]
    comps: list[Comparator] = []
    for part in raw.split():
        match = re.match(r"^(>=|<=|>|<|=)(.+)$", part)
        if not match:
            raise ValueError(f"invalid constraint: {raw}")
        op = "==" if match.group(1) == "=" else match.group(1)
        comps.append(Comparator(op, Version.parse(match.group(2))))
    if not comps:
        raise ValueError("empty constraint")
    return comps


def satisfies(version: str, constraints: Iterable[str]) -> bool:
    parsed = Version.parse(version)
    return all(comp.matches(parsed) for raw in constraints for comp in parse_constraint(raw))


def highest_satisfying(versions: list[str], constraints: list[str]) -> str | None:
    matches = [v for v in versions if satisfies(v, constraints)]
    if not matches:
        return None
    return str(max(Version.parse(v) for v in matches))


def resolve(dependencies: list[dict]) -> dict:
    requirements: dict[str, list[tuple[str, str]]] = {}
    selected: dict[str, str] = {}
    lock: dict[str, dict] = {}

    def add_req(name: str, constraint: str, path: str) -> None:
        parse_constraint(constraint)
        item = (constraint, path)
        bucket = requirements.setdefault(name, [])
        if item not in bucket:
            bucket.append(item)

    for dep in sorted(dependencies, key=lambda d: d["name"]):
        add_req(dep["name"], dep["version"], f"pipeline -> {dep['name']}@{dep['version']}")

    def choose(name: str, reqs: dict[str, list[tuple[str, str]]]) -> str:
        versions = [r["version"] for r in list_artifacts(name)]
        constraints = [c for c, _ in reqs.get(name, [])]
        picked = highest_satisfying(versions, constraints)
        if not picked:
            detail = "; ".join(f"{path} requires {constraint}" for constraint, path in reqs.get(name, []))
            raise ResolutionError(f"version conflict for {name}: {detail}")
        return picked

    for _ in range(100):
        before_requirements = json.dumps(requirements, sort_keys=True)
        selected = {name: choose(name, requirements) for name in sorted(requirements)}
        lock = {}
        visiting: list[str] = []
        visited: set[str] = set()

        def walk(name: str) -> None:
            version = selected[name]
            node = f"{name}@{version}"
            if node in visiting:
                cycle = visiting[visiting.index(node):] + [node]
                raise DependencyCycleError("dependency cycle: " + " -> ".join(cycle))
            if name in visited:
                return
            visiting.append(node)
            meta = get_artifact(name, version)
            if not meta:
                raise ResolutionError(f"artifact disappeared during resolution: {node}")
            deps = meta.get("deps") or []
            if isinstance(deps, str):
                deps = json.loads(deps)
            deps = sorted(deps, key=lambda d: d["name"])
            lock[name] = {
                "name": name,
                "version": version,
                "sha256": meta["sha256"],
                "size": meta["size"],
                "deps": deps,
            }
            for dep in deps:
                add_req(dep["name"], dep["version"], f"{node} -> {dep['name']}@{dep['version']}")
                if dep["name"] in selected:
                    walk(dep["name"])
            visiting.pop()
            visited.add(name)

        for name in sorted(selected):
            walk(name)

        after_requirements = json.dumps(requirements, sort_keys=True)
        if before_requirements == after_requirements:
            return {"dependencies": [lock[name] for name in sorted(lock)]}

    raise ResolutionError("dependency graph did not converge")
