#!/usr/bin/env bash
# Run a real Ubuntu 26.10 kernel/userland in a disposable nested VM.
set -euo pipefail
if [ "${GITHUB_ACTIONS:-}" != true ] || [ -z "${RUNNER_TEMP:-}" ]; then
  echo 'This harness is restricted to disposable GitHub-hosted runners.' >&2; exit 2
fi
sha="${GITHUB_SHA:?}"
repository="${GITHUB_REPOSITORY:?}"
if ! [[ "$sha" =~ ^[0-9a-f]{40}$ && "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then exit 2; fi
task_root="$(mktemp -d "$RUNNER_TEMP/odicto-ubuntu2610.XXXXXX")"
evidence="$RUNNER_TEMP/odicto-ubuntu2610-evidence"
mkdir -p "$evidence"
cd "$task_root"
ssh_options=(-i "$task_root/key" -p 2222 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
cleanup() {
  if [ -f "$task_root/qemu.pid" ]; then
    vm_pid="$(cat "$task_root/qemu.pid")"
    if [[ "$vm_pid" =~ ^[0-9]+$ ]] && [ -r "/proc/$vm_pid/comm" ] && [[ "$(cat "/proc/$vm_pid/comm")" == qemu-system-* ]]; then kill "$vm_pid" || true; fi
  fi
}
trap cleanup EXIT
test -c /dev/kvm || { echo 'A real KVM guest is required; refusing a container substitution.' >&2; exit 1; }
sudo setfacl -m "u:$(id -un):rw" /dev/kvm
image_url='https://cloud-images.ubuntu.com/stonking/current'
image_name='stonking-server-cloudimg-amd64.img'
curl --fail --location --retry 3 "$image_url/SHA256SUMS" --output SHA256SUMS
curl --fail --location --retry 3 "$image_url/$image_name" --output "$image_name"
awk -v file="$image_name" '$2 == file || $2 == "*"file { print }' SHA256SUMS > image.sha256
test "$(wc -l < image.sha256)" -eq 1
sha256sum --check image.sha256
cp image.sha256 "$evidence/cloud-image.sha256"
ssh-keygen -q -t ed25519 -N '' -f key
cat > user-data <<EOF
#cloud-config
users:
  - name: odicto
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $(cat key.pub)
ssh_pwauth: false
disable_root: true
EOF
printf 'instance-id: odicto-ci-%s\nlocal-hostname: odicto-ci\n' "$GITHUB_RUN_ID" > meta-data
cloud-localds seed.img user-data meta-data
qemu-img create -f qcow2 -F qcow2 -b "$task_root/$image_name" disk.qcow2 20G
cp /usr/share/OVMF/OVMF_VARS_4M.fd firmware-vars.fd
qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -smp 2 -m 4096 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file=firmware-vars.fd \
  -drive file=disk.qcow2,if=virtio -drive file=seed.img,format=raw,if=virtio \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=net0 \
  -display none -serial "file:$evidence/serial.log" -pidfile qemu.pid -daemonize
ready=0
for attempt in $(seq 1 60); do
  if ssh "${ssh_options[@]}" odicto@127.0.0.1 true 2>/dev/null; then ready=1; break; fi
  sleep 5
done
test "$ready" -eq 1 || { echo 'Ubuntu 26.10 guest did not become ready.' >&2; exit 1; }
status=0
ssh "${ssh_options[@]}" odicto@127.0.0.1 bash -s -- "$sha" "$repository" <<'GUEST' | tee "$evidence/guest.log" || status=$?
set -euo pipefail
sha="$1"
repository="$2"
. /etc/os-release
test "$ID" = ubuntu && test "$VERSION_ID" = 26.10
sudo cloud-init status --wait
# The same retries as tools/ci_apt_install.sh (the guest has no checkout yet).
apt_options=(-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
apt_install() {
  for try in 1 2 3; do
    timeout 180 sudo apt-get "${apt_options[@]}" update </dev/null || echo "apt-get update failed (try $try of 3)"
    if timeout 420 sudo DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" install -y "$@" </dev/null; then return 0; fi
    echo "apt-get install failed (try $try of 3)"
    [ "$try" = 3 ] || sleep 30
  done
  return 1
}
apt_install git curl ca-certificates nodejs libportaudio2 pulseaudio xclip xdotool xvfb xauth x11-utils \
  libegl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0
pulseaudio --start --exit-idle-time=-1
pactl load-module module-null-sink sink_name=odicto_ci
pactl set-default-source odicto_ci.monitor
curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
export GITHUB_ACTIONS=true GITHUB_SHA="$sha" RUNNER_TEMP="$HOME/ci-temp" ODICTO_RUNNER_LABEL=ubuntu-26.10-vm
mkdir -p "$RUNNER_TEMP" "$HOME/source"
cd "$HOME/source"
git init -q
git remote add origin "https://github.com/$repository.git"
git fetch --depth 1 origin "$sha"
git checkout --detach FETCH_HEAD
uv python install 3.12
xvfb-run -a env XDG_SESSION_TYPE=x11 WAYLAND_DISPLAY= uv run --no-project --python 3.12 tools/ci_platform_smoke.py \
  --root "$RUNNER_TEMP/installed" --evidence "$RUNNER_TEMP/evidence" --sha "$sha"
cd "$RUNNER_TEMP/installed"
xvfb-run -a env QT_QPA_PLATFORM=xcb .venv/bin/python tools/x11_integration_check.py \
  --output "$RUNNER_TEMP/evidence/x11.json"
GUEST
scp -i "$task_root/key" -P 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -r odicto@127.0.0.1:ci-temp/evidence "$evidence/installed" || status=1
ssh "${ssh_options[@]}" odicto@127.0.0.1 'sudo poweroff' || true
exit "$status"
