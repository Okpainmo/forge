from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from registry.config import cfg


def connect() -> psycopg.Connection:
    return psycopg.connect(cfg("database.dsn"), row_factory=dict_row)


@contextmanager
def db() -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
              id BIGSERIAL PRIMARY KEY,
              name TEXT NOT NULL UNIQUE,
              token_hash TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS artifacts (
              id BIGSERIAL PRIMARY KEY,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              size BIGINT NOT NULL,
              publisher TEXT NOT NULL,
              deps JSONB NOT NULL DEFAULT '[]'::jsonb,
              published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              UNIQUE (name, version)
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_name ON artifacts(name);

            CREATE TABLE IF NOT EXISTS runs (
              id UUID PRIMARY KEY,
              pipeline_name TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              started_at TIMESTAMPTZ,
              finished_at TIMESTAMPTZ,
              lockfile JSONB,
              failure_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS jobs (
              id BIGSERIAL PRIMARY KEY,
              run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TIMESTAMPTZ,
              finished_at TIMESTAMPTZ,
              exit_code INTEGER,
              skipped_reason TEXT,
              UNIQUE(run_id, name)
            );
            """
        )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_run(run_id: str, pipeline_name: str, status: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO runs(id, pipeline_name, status) VALUES (%s, %s, %s)",
            (run_id, pipeline_name, status),
        )


def update_run(run_id: str, status: str, **fields: Any) -> None:
    allowed = {"started_at", "finished_at", "lockfile", "failure_reason"}
    sets = ["status = %s"]
    values: list[Any] = [status]
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"unknown run field {key}")
        sets.append(f"{key} = %s")
        values.append(Jsonb(value, dumps=lambda obj: json.dumps(obj, sort_keys=True)) if key == "lockfile" and value is not None else value)
    values.append(run_id)
    with db() as conn:
        conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = %s", values)


def get_run(run_id: str) -> dict[str, Any] | None:
    with db() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
        if not run:
            return None
        jobs = conn.execute(
            "SELECT name, status, started_at, finished_at, exit_code, skipped_reason FROM jobs WHERE run_id = %s ORDER BY name",
            (run_id,),
        ).fetchall()
        run["jobs"] = jobs
        return run


def upsert_job(run_id: str, name: str, status: str, **fields: Any) -> None:
    allowed = {"started_at", "finished_at", "exit_code", "skipped_reason"}
    cols = ["run_id", "name", "status"]
    vals: list[Any] = [run_id, name, status]
    updates = ["status = EXCLUDED.status"]
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"unknown job field {key}")
        cols.append(key)
        vals.append(value)
        updates.append(f"{key} = EXCLUDED.{key}")
    with db() as conn:
        conn.execute(
            f"INSERT INTO jobs({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT(run_id, name) DO UPDATE SET {', '.join(updates)}",
            vals,
        )


def insert_artifact(row: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO artifacts(name, version, sha256, size, publisher, deps)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (row["name"], row["version"], row["sha256"], row["size"], row["publisher"], Jsonb(row.get("deps", []), dumps=lambda obj: json.dumps(obj, sort_keys=True))),
        )


def get_artifact(name: str, version: str) -> dict[str, Any] | None:
    with db() as conn:
        return conn.execute(
            "SELECT name, version, sha256, size, publisher, deps, published_at FROM artifacts WHERE name = %s AND version = %s",
            (name, version),
        ).fetchone()


def list_versions(name: str) -> list[str]:
    with db() as conn:
        rows = conn.execute("SELECT version FROM artifacts WHERE name = %s", (name,)).fetchall()
    return sorted((r["version"] for r in rows), key=lambda v: tuple(int(p) for p in v.split(".")))


def list_artifacts(name: str) -> list[dict[str, Any]]:
    with db() as conn:
        return conn.execute(
            "SELECT name, version, sha256, size, deps FROM artifacts WHERE name = %s",
            (name,),
        ).fetchall()
