#!/usr/bin/env bash
# A second repo in the dashboard, so video D's table shows what the tool
# actually looks like on a working machine: more than one project, and
# containers in more than one state.
#
#   rig/filler-repo.sh
#
# These containers are scenery. The video never enters them and no caption
# claims anything about them — they exist so the dashboard's STATE and NETWORK
# columns have something to say, and so the "one view across repos" part of
# `jailbee dashboard` is visible rather than asserted.
#
# It costs no second image build: `golden.alias` points at the substrate's
# already-built image (the key exists for exactly this — see docs/config.md).
# The containers are idle, run no agent and no dev server, and cost about
# 165 MB each.
#
# Idempotent: re-running it leaves an existing repo and existing containers
# alone.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RIG_DIR="${RIG_DIR:-$REPO_ROOT/.local/video-rig}"
NAME="${NAME:-invoicer}"
FILLER="${FILLER:-$RIG_DIR/$NAME}"
ORIGIN="${ORIGIN:-${FILLER}.git}"
GOLDEN_ALIAS="${GOLDEN_ALIAS:-jailbee-demo-base}"

say() { printf '\n==> %s\n' "$1"; }
die() {
    echo "error: $*" >&2
    exit 1
}

command -v jailbee >/dev/null || die "jailbee is not on PATH (uv tool install -e $REPO_ROOT)"
incus image alias list --format csv | grep -q "^${GOLDEN_ALIAS}," ||
    die "no image alias '$GOLDEN_ALIAS' — run rig/up.sh first"

if [[ ! -d $FILLER/.git ]]; then
    say "Creating the filler repo at $FILLER"
    git init --quiet --bare --initial-branch=main "$ORIGIN"
    mkdir -p "$FILLER/.jailbee"

    cat > "$FILLER/README.md" <<'MD'
# invoicer

A second project, so the jailbee dashboard in the workflow videos shows what
one actually looks like on a machine with more than one thing going on.

Nothing here is entered on camera.
MD

    # No claude block (agents default to off), no autostart: these containers
    # are scenery and should cost nothing to start. golden.alias reuses the
    # substrate's image, which is what keeps this free.
    cat > "$FILLER/.jailbee/config.yaml" <<YAML
# Scenery for the workflow videos — see website/demo/rig/filler-repo.sh.
container_user:
  uid: -1
  gid: -1

egress_allow: []

defaults:
  memory: 1GiB
  cpu: 1
  network: strict
  storage_pool: default

golden:
  alias: ${GOLDEN_ALIAS}

new:
  clone_from: local
YAML

    git -C "$FILLER" init --quiet --initial-branch=main
    git -C "$FILLER" remote add origin "$ORIGIN"
    git -C "$FILLER" add -A
    git -C "$FILLER" commit --quiet -m "feat: an invoicing service"
    git -C "$FILLER" push --quiet --set-upstream origin main
fi

cd "$FILLER"

say "Applying jailbee to $NAME"
if incus profile show "${NAME}-base" >/dev/null 2>&1; then
    jailbee apply
else
    jailbee init
fi

# Two rows, deliberately different from each other and from the substrate's:
# one running in loose mode (so the NETWORK cell shows the mode and its TTL),
# one stopped (so STATE is not a constant and a parked container reads as the
# cheap thing it is).
if ! jailbee ls -o json 2>/dev/null | grep -q '"feat-invoice-pdf"'; then
    say "Creating feat-invoice-pdf"
    jailbee new feat/invoice-pdf
fi

# `--for 4h` rather than the default TTL: the auto-revert fires after a few
# minutes otherwise, and a row that flips from `loose (2m)` to `strict` in the
# middle of a take reads as a glitch. Re-applied on every run so a rig left
# sitting between sessions still shows loose when the camera rolls.
jailbee net loose feat-invoice-pdf --for 4h

if ! jailbee ls -o json 2>/dev/null | grep -q '"fix-vat-rounding"'; then
    say "Creating fix-vat-rounding"
    jailbee new fix/vat-rounding
    jailbee stop fix-vat-rounding
fi

say "Filler repo ready"
jailbee ls
