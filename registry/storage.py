from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

from psycopg.errors import UniqueViolation

from registry.config import cfg
from registry.metadata import get_artifact, insert_artifact

NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA_RE = re.compile(r"^sha256:([a-fA-F0-9]{64})$")


class ArtifactError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ArtifactError(400, "invalid artifact name")


def validate_semver(version: str) -> None:
    if not SEMVER_RE.match(version):
        raise ArtifactError(400, "version must be semver MAJOR.MINOR.PATCH")


def blob_root() -> Path:
    root = Path(cfg("storage.blob_dir", "/var/lib/forge/blobs"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def blob_path(sha256: str) -> Path:
    return blob_root() / sha256[:2] / sha256


def store_upload(
    *,
    name: str,
    version: str,
    checksum: str,
    fileobj: BinaryIO,
    publisher: str,
    deps: list[dict] | None = None,
) -> dict:
    validate_name(name)
    validate_semver(version)
    match = SHA_RE.match(checksum)
    if not match:
        raise ArtifactError(400, "checksum must be sha256:<64 hex chars>")
    declared = match.group(1).lower()

    if get_artifact(name, version):
        raise ArtifactError(409, f"{name}@{version} already exists")

    hasher = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(prefix="forge-upload-", dir=str(blob_root()))
    try:
        with os.fdopen(fd, "wb") as tmp:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                hasher.update(chunk)
                tmp.write(chunk)
        actual = hasher.hexdigest()
        if actual != declared:
            raise ArtifactError(400, f"checksum mismatch: expected {declared}, actual {actual}")

        final = blob_path(actual)
        final.parent.mkdir(parents=True, exist_ok=True)
        if not final.exists():
            shutil.move(tmp_name, final)
            tmp_name = ""
        row = {
            "name": name,
            "version": version,
            "sha256": actual,
            "size": size,
            "publisher": publisher,
            "deps": deps or [],
        }
        try:
            insert_artifact(row)
        except UniqueViolation:
            raise ArtifactError(409, f"{name}@{version} already exists") from None
        return row
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)
