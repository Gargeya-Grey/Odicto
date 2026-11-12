#!/usr/bin/env bash
# Install Ubuntu packages on a CI runner, with retries. Usage: ci_apt_install.sh PACKAGE...
#
# On 11 Nov 2026 the hosted Ubuntu runners failed to resolve the mirror names now and then,
# and two fallback mirrors answered 404 for some amd64v3 packages. One plain apt-get then
# failed or stalled until the job limit (40 min). Packages that a try downloads stay in
# apt's cache, so the next try fetches only the rest. Each try has its own time limit.
set -uo pipefail
if [ "$#" -eq 0 ]; then echo "usage: $0 PACKAGE..." >&2; exit 2; fi
options=(-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
for try in 1 2 3; do
  # A partial index update can still install; the install decides.
  timeout 180 sudo apt-get "${options[@]}" update </dev/null || echo "apt-get update failed (try $try of 3)"
  if timeout 420 sudo DEBIAN_FRONTEND=noninteractive apt-get "${options[@]}" install -y "$@" </dev/null; then
    exit 0
  fi
  echo "apt-get install failed (try $try of 3)"
  [ "$try" = 3 ] || sleep 30
done
exit 1
