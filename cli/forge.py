from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import httpx
import typer

app = typer.Typer(help="Forge CI/CD and artifact registry CLI")
CONFIG_DIR = Path.home() / ".config" / "forge"
CONFIG_PATH = CONFIG_DIR / "config.json"


def load_cli_config() -> dict:
    if not CONFIG_PATH.exists():
        raise typer.BadParameter("not logged in; run forge login <url>")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_cli_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


def headers() -> dict[str, str]:
    data = load_cli_config()
    return {"Authorization": f"Bearer {data['token']}"}


def base_url() -> str:
    return load_cli_config()["url"].rstrip("/")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@app.command()
def login(url: str) -> None:
    normalized_url = url.rstrip("/")
    token = typer.prompt("Bearer token", hide_input=True)
    try:
        response = httpx.get(
            f"{normalized_url}/whoami",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"login failed: could not reach {normalized_url}: {exc}")
        raise typer.Exit(1) from exc
    if response.status_code >= 400:
        typer.echo(f"login failed: {response.text}")
        raise typer.Exit(1)
    save_cli_config({"url": normalized_url, "token": token})
    typer.echo(f"logged in to {normalized_url} as {response.json()['name']}")


@app.command("run")
def run_pipeline(pipeline_yaml: Path) -> None:
    with pipeline_yaml.open("rb") as handle:
        response = httpx.post(
            f"{base_url()}/runs",
            headers=headers(),
            files={"pipeline": (pipeline_yaml.name, handle, "application/x-yaml")},
            timeout=None,
        )
    if response.status_code >= 400:
        typer.echo(response.text)
        raise typer.Exit(1)
    typer.echo(response.json()["run_id"])


@app.command()
def logs(run_id: str, follow: bool = typer.Option(False, "--follow", "-f")) -> None:
    url = f"{base_url()}/runs/{run_id}/logs"
    with httpx.stream("GET", url, params={"follow": str(follow).lower()}, timeout=None) as response:
        if response.status_code >= 400:
            typer.echo(response.text)
            raise typer.Exit(1)
        for raw in response.iter_lines():
            if not raw.startswith("data: "):
                continue
            event = json.loads(raw.removeprefix("data: "))
            typer.echo(f"{event['ts']} [{event['job']}] {event['line']}")


@app.command()
def publish(
    path: Path,
    name: str = typer.Option(..., "--name"),
    version: str = typer.Option(..., "--version"),
    deps: Optional[Path] = typer.Option(None, "--deps", help="Optional JSON dependency list file"),
) -> None:
    digest = sha256_file(path)
    deps_json = deps.read_text(encoding="utf-8") if deps else "[]"
    with path.open("rb") as handle:
        response = httpx.post(
            f"{base_url()}/artifacts/{name}/{version}",
            headers=headers(),
            data={"checksum": f"sha256:{digest}", "deps": deps_json},
            files={"file": (path.name, handle, "application/octet-stream")},
            timeout=None,
        )
    if response.status_code >= 400:
        typer.echo(response.text)
        raise typer.Exit(1)
    typer.echo(json.dumps(response.json(), indent=2, sort_keys=True))


@app.command()
def resolve(pipeline_yaml: Path) -> None:
    with pipeline_yaml.open("rb") as handle:
        response = httpx.post(
            f"{base_url()}/resolve",
            headers=headers(),
            files={"pipeline": (pipeline_yaml.name, handle, "application/x-yaml")},
            timeout=None,
        )
    if response.status_code >= 400:
        typer.echo(response.text)
        raise typer.Exit(1)
    typer.echo(json.dumps(response.json(), indent=2, sort_keys=True))


@app.command("ls")
def ls_package(package: str) -> None:
    response = httpx.get(f"{base_url()}/artifacts/{package}", timeout=None)
    if response.status_code >= 400:
        typer.echo(response.text)
        raise typer.Exit(1)
    for version in response.json()["versions"]:
        typer.echo(version)


if __name__ == "__main__":
    app()
