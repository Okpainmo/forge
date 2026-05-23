from __future__ import annotations

import secrets
import typer
from passlib.context import CryptContext

from registry.metadata import db, init_db

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
app = typer.Typer(help="Forge auth administration")


@app.callback()
def main() -> None:
    """Manage Forge bearer tokens."""


def hash_token(token: str) -> str:
    return pwd_context.hash(token)


def verify_token(token: str) -> str | None:
    with db() as conn:
        rows = conn.execute("SELECT name, token_hash FROM tokens ORDER BY id").fetchall()
    for row in rows:
        if pwd_context.verify(token, row["token_hash"]):
            return row["name"]
    return None


def require_identity(auth_header: str | None) -> str:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise PermissionError("missing bearer token")
    identity = verify_token(auth_header.removeprefix("Bearer ").strip())
    if not identity:
        raise PermissionError("invalid bearer token")
    return identity


@app.command("create-token")
def create_token(name: str = typer.Option(..., "--name")) -> None:
    init_db()
    token = "fg_" + secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "INSERT INTO tokens(name, token_hash) VALUES (%s, %s)",
            (name, hash_token(token)),
        )
    typer.echo(token)


if __name__ == "__main__":
    app()
