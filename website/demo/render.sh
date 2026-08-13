#!/usr/bin/env bash
# Render the demo tapes to website/assets/media/.
#
# Needs vhs, ttyd and ffmpeg, none of which exist in a strict-mode
# container — this runs on the host. With no arguments it renders every
# tape; with a scene name it renders one.
#
# Usage:
#   render.sh                          render all four scenes
#   render.sh <scene>                  render one scene (new|parallel|net|git)
#   render.sh <scene> --transcript P   render one scene, but replace its
#                                      scenes/<scene>.sh with `cat P` first —
#                                      see scenes/README.md, "Replacing a
#                                      reconstruction with a real capture".
#                                      The scene script is restored on exit.
set -euo pipefail

SCENES=(new parallel net git)

usage() {
  cat <<'EOF'
Usage: render.sh [scene] [--transcript PATH]

  scene            One of: new, parallel, net, git. Omit to render all four.
  --transcript P   Only valid with a single scene. Swaps scenes/<scene>.sh
                    for a script that `cat`s the file at P (a real captured
                    terminal session — see scenes/README.md), records the
                    tape against that instead, then restores the original
                    scene script. The .tape file itself is never edited.
EOF
}

require_tools() {
  if ! command -v vhs >/dev/null 2>&1; then
    cat >&2 <<'EOF'
error: `vhs` was not found on PATH.

Rendering the demo clips needs three tools that this repo's dev container
deliberately does not have — they run on the host, not in here:

  - vhs    https://github.com/charmbracelet/vhs
  - ttyd   https://github.com/tsl0922/ttyd     (vhs shells out to this)
  - ffmpeg https://ffmpeg.org/                  (vhs shells out to this)

Install all three on the host, then re-run this script there.
EOF
    exit 1
  fi
}

# -- argument parsing --------------------------------------------------
scene=""
transcript=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --transcript)
      if [[ $# -lt 2 ]]; then
        echo "error: --transcript needs a path argument" >&2
        exit 1
      fi
      transcript="$2"
      shift 2
      ;;
    -*)
      echo "error: unknown option '$1'" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "$scene" ]]; then
        echo "error: unexpected argument '$1' (scene already set to '$scene')" >&2
        exit 1
      fi
      scene="$1"
      shift
      ;;
  esac
done

if [[ -n "$transcript" && -z "$scene" ]]; then
  echo "error: --transcript needs exactly one scene to apply to" >&2
  exit 1
fi

if [[ -n "$scene" ]]; then
  match=""
  for candidate in "${SCENES[@]}"; do
    if [[ "$candidate" == "$scene" ]]; then
      match="$candidate"
      break
    fi
  done
  if [[ -z "$match" ]]; then
    echo "error: unknown scene '$scene' (expected one of: ${SCENES[*]})" >&2
    exit 1
  fi
fi

# Resolve --transcript to an absolute path before any `cd` below — a path
# the caller gave relative to their own shell must not silently break once
# this script changes directory.
if [[ -n "$transcript" ]]; then
  if [[ ! -f "$transcript" ]]; then
    echo "error: transcript file not found: $transcript" >&2
    exit 1
  fi
  transcript="$(cd "$(dirname "$transcript")" && pwd)/$(basename "$transcript")"
fi

require_tools

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tapes_dir="$root/tapes"
scenes_dir="$root/scenes"
media_dir="$root/../assets/media"
mkdir -p "$media_dir"
media_dir="$(cd "$media_dir" && pwd)"

if [[ -n "$scene" ]]; then
  scenes_to_render=("$scene")
else
  scenes_to_render=("${SCENES[@]}")
fi

render_one() {
  local name="$1"
  echo "==> Rendering $name.tape"
  (cd "$tapes_dir" && vhs "$name.tape")
}

for name in "${scenes_to_render[@]}"; do
  if [[ -n "$transcript" ]]; then
    scene_script="$scenes_dir/$name.sh"
    backup="$(mktemp)"
    # -p: mktemp's backup file starts at mode 600; without preserving the
    # scene script's mode, restoring it below would silently drop its
    # executable bit.
    cp -p "$scene_script" "$backup"
    restore() {
      mv "$backup" "$scene_script"
    }
    trap restore EXIT
    cat >"$scene_script" <<SCRIPT
#!/usr/bin/env bash
# Temporary stand-in written by render.sh --transcript $transcript.
# Restored to the original reconstruction when render.sh exits.
set -euo pipefail
cat "$transcript"
SCRIPT
    chmod +x "$scene_script"
    render_one "$name"
    restore
    trap - EXIT
  else
    render_one "$name"
  fi
done

echo "==> Output sizes"
for name in "${scenes_to_render[@]}"; do
  for ext in webm mp4; do
    file="$media_dir/$name.$ext"
    if [[ -f "$file" ]]; then
      printf '%-16s %s\n' "$name.$ext" "$(du -h "$file" | cut -f1)"
    else
      printf '%-16s (not produced)\n' "$name.$ext"
    fi
  done
done
