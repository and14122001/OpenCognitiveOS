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
            --python /usr/bin/python3 --no-dev --extra acp --frozen
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
if [[ "$provider" == hermes ]]; then
    mv "$upstream" "$stage/hermes-source"
    args+=(--ro-bind "$stage/hermes-source" "$upstream")
fi
{
    mkdir "$stage/model-runtime"
    curl --fail --location --retry 2 -o "$stage/llama.tar.gz" https://github.com/ggml-org/llama.cpp/releases/download/b12810/llama-b12810-bin-ubuntu-x64.tar.gz
    echo "63b94be37dcc34cb4933554d4f998e9649841b23da7ffc71d1f66d63762464d7  $stage/llama.tar.gz" | sha256sum --check
    tar -xzf "$stage/llama.tar.gz" --strip-components=1 -C "$stage/model-runtime"
    curl --fail --location --retry 2 -o "$stage/model.gguf" https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/09816acd5d99df7be770d85ea30822623dab342c/SmolLM2-135M-Instruct-Q4_K_M.gguf
    echo "2e8040ceae7815abe0dcb3540b9995eaa1fa0d2ca9e797d0a635ae4433c68c2d  $stage/model.gguf" | sha256sum --check
    cp tools/local_agent_model.py "$stage/local_agent_model.py"
    args+=(--ro-bind "$stage/model-runtime" /model-runtime --ro-bind "$stage/model.gguf" /model-fixture.gguf
           --ro-bind "$stage/local_agent_model.py" /local_agent_model.py)
}
if [[ "$provider" == pi ]]; then
    argv+=(--model local-fixture/ocos-fixture --no-tools --no-session --no-extensions --system-prompt "Answer arithmetic briefly.")
fi
cp -r src "$stage/src"
cp tools/provider_handshake.py "$stage/probe.py"
sudo -n systemd-run --quiet --expand-environment=no --wait --collect --pipe \
    -p MemoryMax=2G -p TasksMax=128 -p RuntimeMaxSec=150 -p KillMode=control-group \
    /usr/bin/bwrap --unshare-all --die-with-parent --new-session --clearenv --uid 65534 --gid 65534 \
    --cap-drop ALL "${args[@]}" --ro-bind "$stage/provider" /provider --ro-bind "$stage/src" /src \
    --ro-bind "$stage/probe.py" /probe.py --proc /proc --dev /dev --tmpfs /tmp --chdir /tmp \
    --setenv PATH /usr/bin:/bin --setenv HOME /tmp --setenv PYTHONPATH /src \
    /usr/bin/python3 /probe.py "$provider" "${argv[@]}"
