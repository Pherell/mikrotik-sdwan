#!/bin/sh
# Run the live CHR tests (backend/tests/chr) against a real RouterOS CHR.
#
# Skipped by default everywhere else; this script is the opt-in. It never
# contains the device password: it takes CHR_PASSWORD from the
# environment, or reads the file named by CHR_PASSWORD_FILE.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ -z "${CHR_PASSWORD:-}" ]; then
    PW_FILE=${CHR_PASSWORD_FILE:-"$HOME/workspace/chr-harness/.chr-password"}
    if [ -f "$PW_FILE" ]; then
        CHR_PASSWORD=$(cat "$PW_FILE")
    else
        echo "chr-test: CHR_PASSWORD is unset and no password file at $PW_FILE" >&2
        echo "chr-test: set CHR_PASSWORD or CHR_PASSWORD_FILE (see backend/tests/chr/README.md)" >&2
        exit 1
    fi
fi
export CHR_PASSWORD
export CHR_LIVE=1
export CHR_BASE_URL=${CHR_BASE_URL:-https://127.0.0.1:18443}
export CHR_USER=${CHR_USER:-admin}

# The CHR is a local lab endpoint. A proxy inherited from the surrounding
# environment must not get in the way; on some machines the inherited
# NO_PROXY even breaks httpx client construction (bracketed IPv6 entries
# are not valid URL patterns). Bypass proxies entirely.
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost

PY="$ROOT/backend/.venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "chr-test: no backend venv at $PY - run 'make install' first" >&2
    exit 1
fi

cd "$ROOT/backend"
exec "$PY" -m pytest tests/chr -v "$@"
