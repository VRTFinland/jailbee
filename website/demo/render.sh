#!/usr/bin/env bash
# Post-process a workflow take into a shippable clip, and optionally record it
# first. `rig/up.sh` points here as the last step of the pipeline.
#
#   render.sh                 # post-process every tape's existing raw render
#   render.sh a               # just video A
#   render.sh --record a      # run vhs first, then post-process
#
# Inputs   workflows/<name>.tape          the tape
#          workflows/<name>.{webm,mp4}    the raw render vhs produced
#          cuts/<name>.cuts               optional speed-up list (see below)
# Outputs  ../assets/media/<name>.{webm,mp4}   the shippable clip
#          ../assets/media/<name>.poster.png   its poster frame
#          ../assets/media/<name>-teaser.gif   a looping excerpt, if asked for
#
# The output directory is the one the root .gitignore already names as the
# contract: the raw renders under workflows/ are ignored, and what the page
# references lives in website/assets/media/ and is committed.
#
# THE CUT LIST. One directive per line in `cuts/<name>.cuts`; `#` comments and
# blank lines ignored. Times are seconds into the RAW render, decimals fine.
#
#   speed  <start> <end> <factor>   speed that span up by <factor>
#   poster <t>                      pull the poster frame from <t> (default 1.0)
#   teaser <start> <seconds>        a looping GIF cut from the FINISHED clip
#
# `speed` and `poster` take raw-render times. `teaser` is the exception and
# takes times in the CUT clip's timeline, because that is the thing it is cut
# from — a teaser of the raw render would show dead time the clip removed. The
# script prints both timelines so the two cannot be confused silently.
#
# The teaser exists for README.md and the PyPI page, which have no video
# player: an animated GIF is the only form that renders in both. Terminal video
# has almost no colours, so 20 seconds at 720px costs about 600 KB.
#
# Spans must be ordered and must not overlap. Every sped-up span is stamped
# with a visible `×<factor>` badge in the top-right corner for exactly as long
# as it runs, because the design spec's section 8 permits speeding up dead time
# only when the viewer is told: "Anything left over is sped up with ffmpeg
# setpts *and a visible ×N marker* in the corner."
#
# The times are hand-measured against one specific render, so a cut list is
# only valid for the take it was written for. Re-time it whenever the tape
# changes — `render.sh` re-checks the span against the raw render's duration,
# which catches a list that has gone stale by more than the whole video, but
# nothing can catch a span that has merely drifted by three seconds. So verify
# by eye: every run dumps the frame on each side of every boundary into
# workflows/<name>.boundaries/, which is how you see that a span still starts
# where the command starts and ends before its result lands.
set -euo pipefail

cd "$(dirname "$0")"
readonly WORKFLOWS=workflows
readonly CUTS=cuts
readonly CLIPS=../assets/media
# Plex Mono is the tape's own font (common.tape), so the badge is set in the
# same face as the terminal it sits on top of.
readonly BADGE_FONT=/usr/share/fonts/truetype/ibm-plex/IBMPlexMono-SemiBold.ttf

record=0
if [[ ${1:-} == --record ]]; then
    record=1
    shift
fi

names=("$@")
if [[ ${#names[@]} -eq 0 ]]; then
    for tape in "$WORKFLOWS"/*.tape; do
        base=$(basename "$tape" .tape)
        [[ $base == common ]] && continue
        names+=("$base")
    done
fi

die() {
    echo "error: $*" >&2
    exit 1
}

[[ -f $BADGE_FONT ]] || die "badge font missing: $BADGE_FONT"

duration_of() {
    ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$1"
}

# `bc` is not in the golden image; awk is, and this is all one comparison.
lte() { awk "BEGIN { exit !($1 <= $2) }"; }

mkdir -p "$CLIPS"

for name in "${names[@]}"; do
    tape="$WORKFLOWS/$name.tape"
    [[ -f $tape ]] || die "no such tape: $tape"

    if [[ $record -eq 1 ]]; then
        # The one preflight worth eight minutes: a take against a substrate
        # that still carries the last take's merged work records the opposite
        # of what the tape claims (video A's premise is that /health does not
        # exist yet), and the render only reveals it in the frames. Same
        # SUBSTRATE override as rig/up.sh.
        substrate="${SUBSTRATE:-$(git rev-parse --show-toplevel)/.local/video-rig/jailbee-demo}"
        if [[ -d $substrate/.git ]]; then
            head=$(git -C "$substrate" rev-parse HEAD)
            origin=$(git -C "$substrate" rev-parse origin/main)
            if [[ $head != "$origin" ]]; then
                die "the substrate is at ${head:0:7}, origin/main is at ${origin:0:7} — reset it first:
    git -C $substrate reset --hard origin/main
    jailbee destroy <the container this tape creates> --force"
            fi
        fi
        echo "==> Recording $name"
        (cd "$WORKFLOWS" && VHS_NO_SANDBOX=1 vhs "$name.tape")

        # A render that exits 0 is not a valid take. A tape that ends by
        # pulling the container's work into the host asserts, by doing so, that
        # there was work to pull — so if the substrate did not move, the agent
        # produced nothing and the take is a failure that looks like a success.
        # This is not hypothetical: it happened the moment the seeded Claude
        # credential went stale (the agent's window said "Login expired"), and
        # the tape's own `Wait` on `passed` did not catch it, because `passed`
        # is a substring of `2 failed, 3 passed in 0.21s`.
        if [[ -d ${substrate:-}/.git ]] && grep -q "git pull" "$tape"; then
            if [[ $(git -C "$substrate" rev-parse HEAD) == "$origin" ]]; then
                die "the take is invalid: $substrate is still at origin/main, so
    the pull merged nothing and the agent produced nothing. Check the agent's
    window in the frames. If it says 'Login expired', re-run rig/seed-claude.sh
    and verify inside a container before recording again:
    jailbee exec <container> -- bash -lc 'claude -p \"reply with exactly: ok\"'"
            fi
        fi
    fi

    raw_webm="$WORKFLOWS/$name.webm"
    [[ -s $raw_webm ]] || die "no raw render at $raw_webm — run with --record"
    duration=$(duration_of "$raw_webm")
    echo "==> $name: raw render is ${duration}s"

    poster_at=1.0
    teaser_at=""
    teaser_len=""
    spans=()
    boundaries_cleaned=""
    cutlist="$CUTS/$name.cuts"
    if [[ -f $cutlist ]]; then
        while read -r kind a b c; do
            [[ -z ${kind:-} || $kind == \#* ]] && continue
            case $kind in
            poster) poster_at=$a ;;
            teaser)
                [[ -n ${b:-} ]] || die "$cutlist: 'teaser' needs <start> <seconds>"
                teaser_at=$a
                teaser_len=$b
                ;;
            speed)
                [[ -n ${c:-} ]] || die "$cutlist: 'speed' needs <start> <end> <factor>"
                lte "$b" "$duration" ||
                    die "$cutlist: span ${a}-${b} runs past the ${duration}s render — re-time the list"
                lte "$a" "$b" || die "$cutlist: span ${a}-${b} ends before it starts"
                spans+=("$a $b $c")
                ;;
            *) die "$cutlist: unknown directive '$kind'" ;;
            esac
        done <"$cutlist"
    fi

    if [[ ${#spans[@]} -eq 0 ]]; then
        echo "    no cut list — copying through unchanged"
        for ext in webm mp4; do
            if [[ -s $WORKFLOWS/$name.$ext ]]; then
                cp "$WORKFLOWS/$name.$ext" "$CLIPS/$name.$ext"
            fi
        done
    else
        # Build one filter graph: alternating untouched and sped-up trims,
        # concatenated back together. `setpts` divides the timestamps, which is
        # what makes a span play faster; `drawtext` rides along on the sped
        # segment only, so the badge is on screen exactly while it applies.
        filter=""
        labels=""
        n=0
        cursor=0
        for span in "${spans[@]}"; do
            read -r start end factor <<<"$span"
            lte "$cursor" "$start" || die "$cutlist: span ${start}-${end} overlaps the one before it"
            if lte "$factor" 1; then
                die "$cutlist: factor ${factor} would not speed anything up"
            fi
            # A gap before this span plays at real speed. `lte start cursor`
            # holds only when they are equal here (overlap is already out), so
            # its negation is exactly "there is a gap to emit".
            if ! lte "$start" "$cursor"; then
                filter+="[0:v]trim=start=${cursor}:end=${start},setpts=PTS-STARTPTS[v${n}];"
                labels+="[v${n}]"
                n=$((n + 1))
            fi
            badge="drawtext=fontfile=${BADGE_FONT}:text='×${factor}':fontsize=34"
            badge+=":fontcolor=0xf0a92b:box=1:boxcolor=0x0b0c0e@0.72:boxborderw=14"
            badge+=":x=w-tw-34:y=28"
            filter+="[0:v]trim=start=${start}:end=${end},setpts=(PTS-STARTPTS)/${factor},${badge}[v${n}];"
            labels+="[v${n}]"
            n=$((n + 1))
            cursor=$end
            echo "    ×${factor} over ${start}s–${end}s"

            # The frames that decide whether the span is still timed right: the
            # one the speed-up starts on, and the one it hands back on.
            # Fresh each run: a stale frame from a previous cut list, sitting
            # in the same directory under a different timestamp, is exactly the
            # kind of thing that gets trusted by mistake.
            [[ -n ${boundaries_cleaned:-} ]] || rm -rf "$WORKFLOWS/$name.boundaries"
            boundaries_cleaned=1
            mkdir -p "$WORKFLOWS/$name.boundaries"
            # `-ss` AFTER `-i`: output seeking, which decodes from the start
            # and lands on the exact frame. The fast form (`-ss` before `-i`)
            # seeks to the nearest keyframe, and this webm's keyframes are
            # sparse across exactly the static stretches a cut list targets —
            # it silently returns a frame from many seconds earlier, which in a
            # tool whose whole job is verifying a boundary is worse than
            # useless.
            for edge in "$start" "$end"; do
                ffmpeg -v error -y -i "$raw_webm" -ss "$edge" -frames:v 1 \
                    "$WORKFLOWS/$name.boundaries/at-${edge}s.png"
            done
        done
        if ! lte "$duration" "$cursor"; then
            filter+="[0:v]trim=start=${cursor},setpts=PTS-STARTPTS[v${n}];"
            labels+="[v${n}]"
            n=$((n + 1))
        fi
        filter+="${labels}concat=n=${n}:v=1:a=0[out]"

        ffmpeg -v error -y -i "$raw_webm" -filter_complex "$filter" -map '[out]' \
            -c:v libvpx-vp9 -b:v 0 -crf 32 -row-mt 1 "$CLIPS/$name.webm"
        ffmpeg -v error -y -i "$raw_webm" -filter_complex "$filter" -map '[out]' \
            -c:v libx264 -crf 20 -preset slow -pix_fmt yuv420p -movflags +faststart \
            "$CLIPS/$name.mp4"
        echo "    cut: $(duration_of "$raw_webm")s -> $(duration_of "$CLIPS/$name.webm")s"
    fi

    # From the RAW render, not the cut clip: every time in a cut list is a raw
    # timestamp, and a poster must not carry a ×N badge.
    ffmpeg -v error -y -i "$raw_webm" -ss "$poster_at" -frames:v 1 "$CLIPS/$name.poster.png"
    echo "    poster from ${poster_at}s (raw)"

    if [[ -n $teaser_at ]]; then
        # Cut from the finished clip, hence $CLIPS and not $raw_webm. 10fps and
        # a 64-colour palette: this is a terminal, and paying for 25fps or 256
        # colours would triple the size for nothing a reader would notice.
        ffmpeg -v error -y -i "$CLIPS/$name.webm" -ss "$teaser_at" -t "$teaser_len" \
            -vf "fps=10,scale=720:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" \
            "$CLIPS/$name-teaser.gif"
        echo "    teaser ${teaser_len}s from ${teaser_at}s (cut timeline), \
$(du -h "$CLIPS/$name-teaser.gif" | cut -f1)"
    fi
done

echo "==> Clips in website/assets/media/"
