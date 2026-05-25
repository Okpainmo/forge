#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/reset-state.sh --yes [--config config.yaml] [--data-dir /var/lib/forge]

Destroys local Forge runtime state:
  - stops Docker Compose services and removes Compose volumes
  - deletes artifact blobs, logs, and workspaces from the Forge data directory

Safety guard:
  This script only runs when server.public_url in config.yaml points to localhost,
  127.0.0.1, or [::1].
EOF
}

confirm="false"
config_file="config.yaml"
data_dir="/var/lib/forge"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes)
      confirm="true"
      shift
      ;;
    --config)
      config_file="${2:-}"
      if [ -z "$config_file" ]; then
        echo "error: --config requires a path" >&2
        exit 2
      fi
      shift 2
      ;;
    --data-dir)
      data_dir="${2:-}"
      if [ -z "$data_dir" ]; then
        echo "error: --data-dir requires a path" >&2
        exit 2
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ ! -r "$config_file" ]; then
  echo "error: config file not readable: $config_file" >&2
  exit 1
fi

public_url="$(
  python3 - "$config_file" <<'PY'
import re
import sys

path = sys.argv[1]
in_server = False

with open(path, "r", encoding="utf-8") as handle:
    for line in handle:
        if re.match(r"^server\s*:\s*(#.*)?$", line):
            in_server = True
            continue
        if in_server and re.match(r"^[^\s#][^:]*\s*:", line):
            break
        if in_server:
            match = re.match(r"^\s+public_url\s*:\s*(.*?)\s*(#.*)?$", line)
            if match:
                value = match.group(1).strip().strip("\"'")
                print(value)
                break
PY
)"

if [ -z "$public_url" ]; then
  echo "error: server.public_url was not found in $config_file" >&2
  exit 1
fi

case "$public_url" in
  http://localhost|http://localhost:*|https://localhost|https://localhost:*|\
  http://127.0.0.1|http://127.0.0.1:*|https://127.0.0.1|https://127.0.0.1:*|\
  http://[::1]|http://[::1]:*|https://[::1]|https://[::1]:*)
    ;;
  *)
    cat >&2 <<EOF
error: refusing to reset because server.public_url is not localhost:
  server.public_url: $public_url

This guard prevents accidental data loss on shared or production deployments.
For local resets, set server.public_url to http://localhost:8080 first.
EOF
    exit 1
    ;;
esac

cat <<EOF
Forge reset-state will delete:
  - Docker Compose volumes for this project, including Postgres data
  - $data_dir/blobs
  - $data_dir/logs
  - $data_dir/workspaces

Config guard passed:
  server.public_url: $public_url
EOF

if [ "$confirm" != "true" ]; then
  cat <<'EOF'

No changes made.
Run again with --yes to perform the reset.
EOF
  exit 0
fi

docker compose down -v

remove_path() {
  local path="$1"
  if [ ! -e "$path" ]; then
    return
  fi
  if [ -w "$(dirname "$path")" ]; then
    rm -rf "$path"
  else
    sudo rm -rf "$path"
  fi
}

remove_path "$data_dir/blobs"
remove_path "$data_dir/logs"
remove_path "$data_dir/workspaces"

mkdir -p "$data_dir"

cat <<'EOF'
Forge state reset complete.

Next:
  docker compose up -d
  docker compose exec api python -m registry.auth create-token --name admin
  forge login http://localhost:8080
EOF
