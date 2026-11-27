#!/usr/bin/env bash
# Trusted fixed-command acceptance probe on a disposable Linux runner.
set -euo pipefail
if [[ "$(stat -fc %T /sys/fs/cgroup)" != cgroup2fs ]]; then
    echo 'Missing unified cgroup v2 mount' >&2
    exit 1
fi
unit="ocos-acceptance-$$"
work=$(mktemp -d)
trap 'rm -r -- "$work"' EXIT
mkdir -m 0777 "$work/workspace"
touch "$work/host-only"
export OCOS_HOST_CANARY=host-only
cat >"$work/probe.py" <<'PY'
import os
import socket
import sys
from pathlib import Path

assert not Path(sys.argv[1]).exists()
assert not Path("/sys/fs/cgroup").exists()
assert "OCOS_HOST_CANARY" not in os.environ
assert os.getuid() != 0
assert int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                if line.startswith("CapEff:")), 16) == 0
assert {name for _, name in socket.if_nameindex()} <= {"lo"}
Path("/work/output").write_text("verified")
print("bubblewrap: workspace write, host path hidden, private network, environment and capabilities verified")
PY
# The parent reads the kernel's effective limits inside the transient unit.
sudo -n systemd-run --quiet --unit="$unit" --wait --collect --pipe \
    -p MemoryMax=128M -p TasksMax=16 -p CPUQuota=50% -p RuntimeMaxSec=30 \
    -p KillMode=control-group /bin/bash -eu -c '
        group=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)
        test "$(cat "$group/memory.max")" = 134217728
        test "$(cat "$group/pids.max")" = 16
        read -r quota period < "$group/cpu.max"
        test "$((quota * 2))" -eq "$period"
        echo "cgroup v2: memory.max=134217728 pids.max=16 cpu=50%"
        args=(--ro-bind /usr /usr)
        for path in /bin /lib /lib64; do
            if test -e "$path"; then args+=(--ro-bind "$path" "$path"); fi
        done
        exec bwrap --unshare-all --die-with-parent --new-session --clearenv \
            --uid 65534 --gid 65534 --cap-drop ALL "${args[@]}" \
            --proc /proc --dev /dev --tmpfs /tmp --bind "$1/workspace" /work \
            --ro-bind "$1/probe.py" /probe.py --chdir /work \
            /usr/bin/python3 -I /probe.py "$1/host-only"
    ' bash "$work"
test "$(cat "$work/workspace/output")" = verified
