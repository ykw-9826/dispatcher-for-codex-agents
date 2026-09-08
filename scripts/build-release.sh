#!/bin/sh
# Rebuild one immutable version from this independent repository and uv.lock.
set -eu
umask 077
DCA_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$DCA_ROOT"
test -d .git
test -z "$(git status --porcelain=v1)"
DCA_COMMIT=$(git rev-parse HEAD)
DCA_RELEASE="$DCA_ROOT/releases/$DCA_COMMIT"
test ! -e "$DCA_RELEASE"
mkdir "$DCA_RELEASE" "$DCA_RELEASE/build-source"
git archive --format=tar --output="$DCA_RELEASE/source.tar" HEAD
tar -xf "$DCA_RELEASE/source.tar" -C "$DCA_RELEASE/build-source"
"$DCA_ROOT/scripts/dca-env.sh" env UV_PROJECT_ENVIRONMENT="$DCA_RELEASE/venv" \
  "${DCA_UV_EXECUTABLE:-uv}" sync --project "$DCA_RELEASE/build-source" \
  --frozen --offline --no-editable --no-dev
"$DCA_RELEASE/venv/bin/python" -I -B -c \
  'import sys; import dispatcher_for_codex_agents as d; print(d.__version__, d.__file__, sys.base_prefix)'
printf '%s\n' "$DCA_RELEASE"
