#!/bin/bash
# Invoked inside a fresh bounded transient service. Parameters are controller-owned.
set -euo pipefail
group="/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)"
test "$(cat "$group/memory.max")" = 134217728
test "$(cat "$group/pids.max")" = 16
read -r quota period < "$group/cpu.max"
test "$((quota * 2))" -eq "$period"
exec 8<"$1"
exec 9>"$2"
mounts=(--ro-bind /usr /usr)
for path in /bin /lib /lib64; do
    if [[ -e "$path" ]]; then mounts+=(--ro-bind "$path" "$path"); fi
done
program=(/usr/bin/python3 -I -B /task.py)
if [[ $# -eq 3 ]]; then
    exec 7<"$3"
    mounts+=(--ro-bind-data 7 /candidate)
    program+=(/candidate)
fi
exec /usr/bin/bwrap --info-fd 9 --unshare-all --unshare-user --disable-userns --die-with-parent --new-session --clearenv \
    --uid 65534 --gid 65534 --cap-drop ALL "${mounts[@]}" --proc /proc --dev /dev \
    --size 16777216 --perms 0777 --tmpfs /work --size 4194304 --tmpfs /tmp \
    --ro-bind-data 8 /task.py --chdir /work --setenv HOME /work --setenv PATH /usr/bin:/bin \
    "${program[@]}"
