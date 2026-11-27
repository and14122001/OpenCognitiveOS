#!/usr/bin/env bash
set -euo pipefail
exec /bin/bash "$(dirname "$0")/../src/opencognitiveos/execution/verify_linux_isolation.sh" "$@"
