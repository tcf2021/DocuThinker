#!/usr/bin/env bash
# Test substitutes avoid model downloads; block Python sockets except local Milvus.
# This is a pytest guard, not OS-level network isolation for native libraries.
set -euo pipefail
cd "$(dirname "$0")/.."
test_python="${DOCUTHINKER_TEST_PYTHON:-$PWD/.venv/bin/python}"
export OTEL_SDK_DISABLED=true CREWAI_TELEMETRY_ENABLED=false HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
export NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1
exec "$test_python" -m pytest -q tests --disable-socket --allow-hosts=127.0.0.1,localhost,::1 "$@"
