#!/usr/bin/env bash
set -euo pipefail
provider=$1
dest="$RUNNER_TEMP/ocos-provider"
mkdir -p "$dest"
case "$provider" in
    opencode)
        url=https://github.com/anomalyco/opencode/releases/download/v1.19.6/opencode-linux-x64.tar.gz
        digest=47734e08968b30cc22a0bffb6d9feeed76ad4aa743213a12122040392d09a057
        ;;
    pi)
        url=https://github.com/earendil-works/pi/releases/download/v1.6.1/pi-linux-x64.tar.gz
        digest=3c7f00715eb512e165c0da8cad314d73c83b54d5bf7748be4adc618fc8357b68
        ;;
    hermes)
        upstream="$RUNNER_TEMP/hermes-source"
        git init -q "$upstream"
        git -C "$upstream" fetch --depth=1 https://github.com/NousResearch/hermes-agent.git 8fc0374447313e276f6b9a2764bfc750190c966a
        git -C "$upstream" checkout --detach FETCH_HEAD
        test "$(git -C "$upstream" rev-parse HEAD)" = 8fc0374447313e276f6b9a2764bfc750190c966a
        /usr/bin/python3 -m venv "$RUNNER_TEMP/resolver"
        "$RUNNER_TEMP/resolver/bin/pip" install 'uv==0.11.2'
        UV_PROJECT_ENVIRONMENT="$dest/venv" "$RUNNER_TEMP/resolver/bin/uv" sync --project "$upstream" \
            --python /usr/bin/python3 --no-dev --no-editable --extra acp --frozen
        ;;
    *) exit 2 ;;
esac
if [[ "$provider" != hermes ]]; then
    curl --fail --location --retry 2 --output "$dest/release.tar.gz" "$url"
    echo "$digest  $dest/release.tar.gz" | sha256sum --check
    tar -xzf "$dest/release.tar.gz" --no-same-owner -C "$dest"
fi
args=(--ro-bind /usr /usr)
for path in /bin /lib /lib64; do
    if [[ -e "$path" ]]; then args+=(--ro-bind "$path" "$path"); fi
done
case "$provider" in
    pi) binary=$(find "$dest" -type f -name pi -print -quit); argv=("/provider/${binary#"$dest/"}" --mode rpc) ;;
    opencode) binary=$(find "$dest" -type f -name opencode -print -quit); argv=("/provider/${binary#"$dest/"}" acp) ;;
    hermes) argv=(/provider/venv/bin/python -c 'from acp_adapter.entry import main; main()') ;;
esac
# Open read-only bind sources before dropping privileges; fixture data only.
stage=$(mktemp -d /tmp/ocos-provider.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
chmod 0755 "$stage"
mv "$dest" "$stage/provider"
cp -r src "$stage/src"
cp tools/provider_handshake.py "$stage/probe.py"
sudo -n systemd-run --quiet --expand-environment=no --wait --collect --pipe \
    -p MemoryMax=2G -p TasksMax=128 -p RuntimeMaxSec=90 -p KillMode=control-group \
    /usr/bin/bwrap --unshare-all --die-with-parent --new-session --clearenv --uid 65534 --gid 65534 \
    --cap-drop ALL "${args[@]}" --ro-bind "$stage/provider" /provider --ro-bind "$stage/src" /src \
    --ro-bind "$stage/probe.py" /probe.py --proc /proc --dev /dev --tmpfs /tmp --chdir /tmp \
    --setenv PATH /usr/bin:/bin --setenv HOME /tmp --setenv PYTHONPATH /src \
    /usr/bin/python3 /probe.py "$provider" "${argv[@]}"
