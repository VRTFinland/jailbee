#!/bin/bash
# install.sh — slim LXC/Incus plumbing for the jailbee golden image.
#
# Run inside a fresh Ubuntu container by `jailbee base build`. Almost all
# feature provisioning has moved to /provision/install.d/*.sh — this
# script only owns the plumbing that every container needs regardless of
# stack (user creation, sudoers, SSH_AUTH_SOCK passthrough, etc.).
#
# Environment variables (passed via `incus exec --env`):
#   CONTAINER_UID, CONTAINER_GID — uid/gid for the dev user
#   JAILBEE_USER_HOME                — = /home/dev (constant, defaulted below)
#   JAILBEE_PROVISION_DIR            — = /provision (constant, defaulted below)
# Plus per-snippet env (JAVA_PACKAGE, NODE_MAJOR, PYTHON_VERSION,
# EXTRA_APT_PACKAGES, and any golden.provision_env additions) passed
# straight through to snippets.
#
# The unix username inside the container is hardcoded to "dev".
set -euo pipefail

# Exported so install.d/* snippets (which run as `bash "$f"`, a fresh
# process) inherit it. CONTAINER_USER is intentionally not passed via
# `incus exec --env` — it's an internal detail of the golden image.
export CONTAINER_USER=dev

: "${CONTAINER_UID:?CONTAINER_UID required}"
: "${CONTAINER_GID:?CONTAINER_GID required}"
: "${JAILBEE_USER_HOME:=/home/dev}"
: "${JAILBEE_PROVISION_DIR:=/provision}"

# Ubuntu's unattended-upgrade machinery is a liability in a dev container.
# apt-daily.timer fires within minutes of every boot, so it takes the dpkg
# lock out from under whoever is installing something — including this
# script, moments from now — and an apt run still in flight at shutdown
# blocks systemd, which can burn the whole clean-shutdown budget `jailbee`
# gives a stop and leave the container Running. Nothing in a branch
# container wants surprise background upgrades: the image is rebuilt by
# `jailbee base build` instead.
#
# Masked rather than disabled: `apt-get install` of anything that ships
# these units re-enables a merely-disabled timer, and masking survives that.
# The timers are stopped here; the services deliberately are not, since
# SIGTERM to an apt run mid-dpkg is how an image gets a broken package
# database. If one is running, the apt-get below fails loudly on the lock.
echo "==> Masking Ubuntu's automatic apt machinery"
systemctl stop apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl mask \
    apt-daily.timer apt-daily-upgrade.timer \
    apt-daily.service apt-daily-upgrade.service \
    unattended-upgrades.service 2>/dev/null || true

# Ubuntu mounts /tmp as a tmpfs sized at half the host's RAM. In a container
# that memory is charged to the container's cgroup, so a large temp file
# exhausts RAM long before /tmp looks full. Masked so /tmp is ordinary disk;
# `container.tmpfs` opts back in with a size cap, mounted by the Incus profile.
echo "==> Masking tmp.mount (/tmp stays on disk)"
systemctl mask tmp.mount 2>/dev/null || true

# The default build image (images:ubuntu/<ver>) has no cloud-init, and then
# this is a no-op. An image that does run it rewrites the apt sources on first
# boot, while this script runs: an index refresh that finishes before the
# rewrite leaves package lists the install below finds nothing in ("Unable to
# locate package"), so wait for cloud-init to finish first. Its exit status reports
# module errors, not readiness, and is not this script's concern; the
# timeout keeps a wedged cloud-init from stalling provisioning forever.
if command -v cloud-init >/dev/null 2>&1; then
    echo "==> Waiting for cloud-init to finish"
    timeout 300 cloud-init status --wait >/dev/null 2>&1 || true
fi

echo "==> Updating apt cache"
apt-get update -y

# apparmor: a container installing an LSM's userspace looks odd, but it is
# the only way the dev user gets a usable user namespace. Ubuntu hosts set
# kernel.apparmor_restrict_unprivileged_userns=1, under which an
# unprivileged unshare(CLONE_NEWUSER) must be allowed by AppArmor. Incus
# stacks two labels on a container process — the host's and the
# container's own policy namespace's — and each must allow it. The
# container's namespace stays empty unless something in here loads policy,
# so without this package Chrome's zygote aborts (credentials.cc:
# Permission denied), and bwrap and rootless podman fail alike. The package
# ships the `unprivileged_userns` fallback plus per-app profiles (`chrome`,
# `firefox`, ...) whose paths match where jailbee's browsers live, and
# apparmor.service reloads them on every boot. The fallback strips all
# capabilities inside the new namespace, so Chromium's sandbox needs the
# per-app profile — on both sides: an app at a path the host has no
# profile for stays broken whatever is loaded here (docs/config.md#apps).
#
# kitty-terminfo: `jailbee shell` from a kitty terminal carries
# TERM=xterm-kitty into the container, and ncurses-term ships only `kitty`,
# not `xterm-kitty`; without the entry tmux refuses to start and curses
# tools degrade. `terminal.kitty` still mounts the host's file over it, for
# containers built from images that predate this package.
echo "==> Installing LXC plumbing apt packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    apt-transport-https ca-certificates curl gnupg lsb-release wget \
    sudo locales tzdata \
    git make tmux \
    build-essential libssl-dev pkg-config \
    ripgrep fd-find jq htop \
    openssh-server kitty-terminfo \
    apparmor \
    inotify-tools

# xterm-ghostty: Ghostty exports TERM=xterm-ghostty. ncurses-term (6.6) ships
# the entry only as `ghostty`, and Ubuntu's older ncurses-term not at all, so
# tmux inside the container refuses to start ("missing or unsuitable
# terminal"). Compile the entry under both names from `infocmp -x ghostty`
# (ncurses-term 6.6), into the system database every user reads.
echo "==> Installing the xterm-ghostty terminfo"
tic -x - <<'TERMINFO'
xterm-ghostty|ghostty|Ghostty terminal emulator,
	am, bce, ccc, hs, km, mc5i, mir, msgr, npc, xenl, AX, XF, XT,
	colors#0x100, cols#80, it#8, lines#24, pairs#0x7fff,
	acsc=++\,\,--..00``aaffgghhiijjkkllmmnnooppqqrrssttuuvvwwxxyyzz{{||}}~~,
	bel=^G, bold=\E[1m, cbt=\E[Z, civis=\E[?25l,
	clear=\E[H\E[2J, cnorm=\E[?12l\E[?25h, cr=\r,
	csr=\E[%i%p1%d;%p2%dr, cub=\E[%p1%dD, cub1=^H,
	cud=\E[%p1%dB, cud1=\n, cuf=\E[%p1%dC, cuf1=\E[C,
	cup=\E[%i%p1%d;%p2%dH, cuu=\E[%p1%dA, cuu1=\E[A,
	cvvis=\E[?12;25h, dch=\E[%p1%dP, dch1=\E[P, dim=\E[2m,
	dl=\E[%p1%dM, dl1=\E[M, dsl=\E]2;\007, ech=\E[%p1%dX,
	ed=\E[J, el=\E[K, el1=\E[1K, flash=\E[?5h$<100/>\E[?5l,
	fsl=^G, home=\E[H, hpa=\E[%i%p1%dG, ht=^I, hts=\EH,
	ich=\E[%p1%d@, ich1=\E[@, il=\E[%p1%dL, il1=\E[L, ind=\n,
	indn=\E[%p1%dS,
	initc=\E]4;%p1%d;rgb:%p2%{255}%*%{1000}%/%2.2X/%p3%{255}%*%{1000}%/%2.2X/%p4%{255}%*%{1000}%/%2.2X\E\\,
	invis=\E[8m, kDC=\E[3;2~, kEND=\E[1;2F, kHOM=\E[1;2H,
	kIC=\E[2;2~, kLFT=\E[1;2D, kNXT=\E[6;2~, kPRV=\E[5;2~,
	kRIT=\E[1;2C, kbs=^?, kcbt=\E[Z, kcub1=\EOD, kcud1=\EOB,
	kcuf1=\EOC, kcuu1=\EOA, kdch1=\E[3~, kend=\EOF, kent=\EOM,
	kf1=\EOP, kf10=\E[21~, kf11=\E[23~, kf12=\E[24~,
	kf13=\E[1;2P, kf14=\E[1;2Q, kf15=\E[1;2R, kf16=\E[1;2S,
	kf17=\E[15;2~, kf18=\E[17;2~, kf19=\E[18;2~, kf2=\EOQ,
	kf20=\E[19;2~, kf21=\E[20;2~, kf22=\E[21;2~,
	kf23=\E[23;2~, kf24=\E[24;2~, kf25=\E[1;5P, kf26=\E[1;5Q,
	kf27=\E[1;5R, kf28=\E[1;5S, kf29=\E[15;5~, kf3=\EOR,
	kf30=\E[17;5~, kf31=\E[18;5~, kf32=\E[19;5~,
	kf33=\E[20;5~, kf34=\E[21;5~, kf35=\E[23;5~,
	kf36=\E[24;5~, kf37=\E[1;6P, kf38=\E[1;6Q, kf39=\E[1;6R,
	kf4=\EOS, kf40=\E[1;6S, kf41=\E[15;6~, kf42=\E[17;6~,
	kf43=\E[18;6~, kf44=\E[19;6~, kf45=\E[20;6~,
	kf46=\E[21;6~, kf47=\E[23;6~, kf48=\E[24;6~,
	kf49=\E[1;3P, kf5=\E[15~, kf50=\E[1;3Q, kf51=\E[1;3R,
	kf52=\E[1;3S, kf53=\E[15;3~, kf54=\E[17;3~,
	kf55=\E[18;3~, kf56=\E[19;3~, kf57=\E[20;3~,
	kf58=\E[21;3~, kf59=\E[23;3~, kf6=\E[17~, kf60=\E[24;3~,
	kf61=\E[1;4P, kf62=\E[1;4Q, kf63=\E[1;4R, kf7=\E[18~,
	kf8=\E[19~, kf9=\E[20~, khome=\EOH, kich1=\E[2~,
	kind=\E[1;2B, kmous=\E[<, knp=\E[6~, kpp=\E[5~,
	kri=\E[1;2A, mgc=\E[?69l, oc=\E]104\007, op=\E[39;49m,
	rc=\E8, rep=%p1%c\E[%p2%{1}%-%db, rev=\E[7m, ri=\EM,
	rin=\E[%p1%dT, ritm=\E[23m, rmacs=\E(B, rmam=\E[?7l,
	rmcup=\E[?1049l, rmir=\E[4l, rmkx=\E[?1l\E>, rmso=\E[27m,
	rmul=\E[24m, rs1=\E]\E\\\Ec, sc=\E7,
	setab=\E[%?%p1%{8}%<%t4%p1%d%e%p1%{16}%<%t10%p1%{8}%-%d%e48;5;%p1%d%;m,
	setaf=\E[%?%p1%{8}%<%t3%p1%d%e%p1%{16}%<%t9%p1%{8}%-%d%e38;5;%p1%d%;m,
	sgr=%?%p9%t\E(0%e\E(B%;\E[0%?%p6%t;1%;%?%p2%t;4%;%?%p1%p3%|%t;7%;%?%p5%t;2%;%?%p7%t;8%;m,
	sgr0=\E(B\E[m, sitm=\E[3m, smacs=\E(0, smam=\E[?7h,
	smcup=\E[?1049h, smglp=\E[?69h\E[%i%p1%ds,
	smglr=\E[?69h\E[%i%p1%d;%p2%ds,
	smgrp=\E[?69h\E[%i;%p1%ds, smir=\E[4h, smkx=\E[?1h\E=,
	smso=\E[7m, smul=\E[4m, tbc=\E[3g, tsl=\E]2;,
	u6=\E[%i%d;%dR, u7=\E[6n, u8=\E[?%[;0123456789]c,
	u9=\E[c, vpa=\E[%i%p1%dd, BD=\E[?2004l, BE=\E[?2004h,
	E3=\E[3J, Ms=\E]52;%p1%s;%p2%s\007, PE=\E[201~,
	PS=\E[200~, RV=\E[>c, Se=\E[2 q, Smulx=\E[4:%p1%dm,
	Ss=\E[%p1%d q, TS=\E]2;,
	XM=\E[?1006;1000%?%p1%{1}%=%th%el%;, XR=\E[>0q,
	fd=\E[?1004l, fe=\E[?1004h, kDC3=\E[3;3~, kDC4=\E[3;4~,
	kDC5=\E[3;5~, kDC6=\E[3;6~, kDC7=\E[3;7~, kDN=\E[1;2B,
	kDN3=\E[1;3B, kDN4=\E[1;4B, kDN5=\E[1;5B, kDN6=\E[1;6B,
	kDN7=\E[1;7B, kEND3=\E[1;3F, kEND4=\E[1;4F,
	kEND5=\E[1;5F, kEND6=\E[1;6F, kEND7=\E[1;7F,
	kHOM3=\E[1;3H, kHOM4=\E[1;4H, kHOM5=\E[1;5H,
	kHOM6=\E[1;6H, kHOM7=\E[1;7H, kIC3=\E[2;3~, kIC4=\E[2;4~,
	kIC5=\E[2;5~, kIC6=\E[2;6~, kIC7=\E[2;7~, kLFT3=\E[1;3D,
	kLFT4=\E[1;4D, kLFT5=\E[1;5D, kLFT6=\E[1;6D,
	kLFT7=\E[1;7D, kNXT3=\E[6;3~, kNXT4=\E[6;4~,
	kNXT5=\E[6;5~, kNXT6=\E[6;6~, kNXT7=\E[6;7~,
	kPRV3=\E[5;3~, kPRV4=\E[5;4~, kPRV5=\E[5;5~,
	kPRV6=\E[5;6~, kPRV7=\E[5;7~, kRIT3=\E[1;3C,
	kRIT4=\E[1;4C, kRIT5=\E[1;5C, kRIT6=\E[1;6C,
	kRIT7=\E[1;7C, kUP=\E[1;2A, kUP3=\E[1;3A, kUP4=\E[1;4A,
	kUP5=\E[1;5A, kUP6=\E[1;6A, kUP7=\E[1;7A, kxIN=\E[I,
	kxOUT=\E[O, rmxx=\E[29m, rv=\E\\[>[0-9];[0-9];[0-9]c,
	setal=\E[58:2::%p1%{65536}%/%d:%p1%{256}%/%{255}%&%d:%p1%{255}%&%dm,
	smxx=\E[9m, xm=\E[<%i%p3%d;%p1%d;%p2%d;%?%p4%tM%em%;,
	xr=\EP>\\|ghostty %[0-9.]([[:print:]])*\E\\\\,
TERMINFO

# tzdata installs /etc/localtime as a symlink (-> /usr/share/zoneinfo/Etc/UTC).
# The <repo>-binds profile bind-mounts the host's /etc/localtime onto this
# path, but LXC refuses to mount over a symlink destination and
# the container fails to start with "Too many levels of symbolic links".
# Replace the symlink with a regular-file copy of UTC: the bind-mount
# supplies the host's zone at runtime, and a host without /etc/localtime
# (no mount) still leaves the container a valid zone rather than an empty
# file.
echo "==> Replacing /etc/localtime symlink with a regular UTC file"
rm -f /etc/localtime
cp /usr/share/zoneinfo/Etc/UTC /etc/localtime

# Ubuntu cloud images ship with a default `ubuntu` user at UID 1000 / GID 1000.
# When CONTAINER_UID/GID == 1000 (the common case — host user UID), useradd/
# groupadd below collide and silently fail (the `|| true` mask was hiding this,
# leaving subsequent `chown dev:dev` calls to fail with "invalid user").
# Remove the default user/group first so the dev user can claim the UID.
if getent passwd ubuntu >/dev/null; then
    echo "==> Removing default 'ubuntu' user (conflicts with CONTAINER_UID)"
    userdel -r ubuntu 2>/dev/null || true
fi
getent group ubuntu >/dev/null && groupdel ubuntu 2>/dev/null || true

# A host GID can already name a system group in the image: macOS gives its
# users GID 20 ("staff"), which Ubuntu reserves for `dialout`, so a Linux VM
# created from a Mac account hands that GID through. Only the number matters
# for file ownership across the bind mounts, so the existing group is renamed
# to the dev user's rather than failing groupadd on a duplicate GID. That can
# rename a group other image packages refer to by name (`dialout`, or `users`
# for a host whose primary GID is 100); inside a dev container that is the
# lesser cost.
echo "==> Creating user ${CONTAINER_USER} (UID=${CONTAINER_UID}, GID=${CONTAINER_GID})"
existing_group="$(getent group "${CONTAINER_GID}" | cut -d: -f1 || true)"
if [ -z "${existing_group}" ]; then
    groupadd -g "${CONTAINER_GID}" "${CONTAINER_USER}"
elif [ "${existing_group}" != "${CONTAINER_USER}" ]; then
    echo "==> GID ${CONTAINER_GID} belongs to '${existing_group}'; renaming it to '${CONTAINER_USER}'"
    groupmod -n "${CONTAINER_USER}" "${existing_group}"
fi
useradd -m -u "${CONTAINER_UID}" -g "${CONTAINER_GID}" \
        -s /bin/bash "${CONTAINER_USER}"

# ~/.local/bin first on PATH for any tool that follows XDG conventions
# (pipx, cargo install --root ~/.local, the ensure-claude.sh step that
# installs claude into ~/.local/bin at jailbee-new time).
cat > /etc/profile.d/local-bin.sh <<'EOF'
export PATH="$HOME/.local/bin:$PATH"
EOF
chmod 0644 /etc/profile.d/local-bin.sh

# Heal a dangling ~/.local/bin/claude at login.
#
# The two halves of the Claude install disagree about lifetime:
# ~/.local/share/claude/versions is a bind mount SHARED by every container of
# a repo (agent_presets.claude_preset's claude-install cache), while
# ~/.local/bin/claude is a per-container symlink pinned to one exact version
# by ensure-claude.sh — which runs at `jailbee new` and never again. Claude's
# own updater prunes old releases from the shared store, so a `claude update`
# in ANY container of the repo can delete the version THIS container points
# at, and the launcher stays dangling for the rest of the container's life
# (`-bash: /home/dev/.local/bin/claude: No such file or directory`).
#
# Repointing it at login covers every path that runs `claude` in a container:
# each goes through a `bash -lc` login shell (jailbee shell, tmux windows,
# autostart steps, `jailbee pr`'s claude invocation, the agent install check).
#
# Two properties this snippet must keep:
#   - Only acts when the launcher is missing or dangling. A healthy pin is
#     left alone, so `claude.auto_update: false` keeps its chosen version.
#   - Prints nothing, ever. pr_ai.ask_claude_for_pr_text parses the stdout of
#     a `bash -lc` login shell as JSON; a chatty snippet would corrupt it.
# The reverse-sorted loop skips a newest-named entry that isn't a usable
# binary (an interrupted download) instead of linking the stub.
cat > /etc/profile.d/jailbee-claude.sh <<'EOF'
if [ ! -x "$HOME/.local/bin/claude" ]; then
    _jb_claude_store="$HOME/.local/share/claude/versions"
    for _jb_claude_v in $(ls -1 "$_jb_claude_store" 2>/dev/null | sort -V -r); do
        if [ -x "$_jb_claude_store/$_jb_claude_v" ]; then
            mkdir -p "$HOME/.local/bin"
            ln -sfn "$_jb_claude_store/$_jb_claude_v" "$HOME/.local/bin/claude"
            break
        fi
    done
    unset _jb_claude_store _jb_claude_v
fi
EOF
chmod 0644 /etc/profile.d/jailbee-claude.sh

# claude-jb: Claude Code through the jailbee LiteLLM proxy (`jailbee litellm`).
#
# `claude` stays native; this is a separate name so Claude's auto-updater and
# PATH order can never break either. It reads /etc/jailbee/litellm.json
# (written by `jailbee new` / `jailbee apply`), picks a profile —
# --profile/-P, then $JAILBEE_LITELLM_PROFILE, then the file's default — sets the
# variables Claude Code reads for a gateway, and execs `claude`. It writes
# nothing and never falls back to native: a missing proxy is an error.
# With the profile's `instructions` set it also appends them to Claude Code's system prompt.
cat > /usr/local/bin/claude-jb <<'EOF'
#!/bin/bash
set -euo pipefail
config="${JAILBEE_LITELLM_CONFIG:-/etc/jailbee/litellm.json}"
die() { printf 'claude-jb: %s\n' "$1" >&2; exit 2; }

profile="${JAILBEE_LITELLM_PROFILE:-}"
args=()          # what the user passed, minus --profile
plain=()         # the same without the append flags that are merged below
append_parts=()  # one "t<text>" or "f<path>" per --append-system-prompt[-file], in order
user_effort=0
have_context=0   # --context/-C given; $context holds its raw value
context=""
want_help=0
while [ $# -gt 0 ]; do
    case "$1" in
        --) args+=("$@"); plain+=("$@"); break ;;
        --profile|-P) [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a name"; profile="$2"; shift 2 ;;
        --profile=*) profile="${1#--profile=}"; [ -n "$profile" ] || die "--profile needs a name"; shift ;;
        -P?*) profile="${1#-P}"; shift ;;
        # -C, never -c: that is Claude's own --continue and passes through.
        --context|-C) [ $# -ge 2 ] || die "--context needs a value"; have_context=1; context="$2"; shift 2 ;;
        --context=*) have_context=1; context="${1#--context=}"; shift ;;
        --help|-h) want_help=1; args+=("$1"); plain+=("$1"); shift ;;
        --effort|--effort=*) user_effort=1; args+=("$1"); plain+=("$1"); shift ;;
        --append-system-prompt|--append-system-prompt-file)
            if [ $# -lt 2 ]; then args+=("$1"); plain+=("$1"); shift; continue; fi
            kind=t
            if [ "$1" = --append-system-prompt-file ]; then kind=f; fi
            args+=("$1" "$2"); append_parts+=("$kind$2"); shift 2 ;;
        --append-system-prompt=*) args+=("$1"); append_parts+=("t${1#--append-system-prompt=}"); shift ;;
        --append-system-prompt-file=*) args+=("$1"); append_parts+=("f${1#--append-system-prompt-file=}"); shift ;;
        *) args+=("$1"); plain+=("$1"); shift ;;
    esac
done

# claude-jb's own options first, then Claude's help. Needs no proxy, key or
# profile: the profile list is a courtesy, shown only when the config reads.
if [ "$want_help" -eq 1 ]; then
    cat <<'HELP'
claude-jb: Claude Code through the jailbee LiteLLM proxy.
Everything not listed here is passed to claude unchanged.

claude-jb options:
  -P, --profile NAME  gateway profile (default: $JAILBEE_LITELLM_PROFILE,
                      then the config's default profile)
  -C, --context SIZE  context window for this session: 272k, 1m, a token
                      count, 'max' (the profile's ceiling) or 'default'.
                      A larger window costs more per request. (-c is
                      Claude's --continue and is not touched.)
  -h, --help          this text, then claude's own help
HELP
    if [ -r "$config" ] && jq -e . "$config" >/dev/null 2>&1; then
        printf '\nProfiles (context window in tokens):\n'
        jq -r '. as $c | .profiles | to_entries[]
            | "  \(.key)\(if .key == $c.default_profile then " (default)" else "" end): window \(.value.context_window), up to \(.value.max_context_window // .value.context_window)"' "$config"
    fi
    printf '\n--- claude --help ---\n'
    exec claude "${args[@]}"
fi

[ -r "$config" ] || die "no LiteLLM proxy configured for this container ($config missing). On the host: \`jailbee litellm up\`, then \`jailbee apply\` in this repo."
jq -e . "$config" >/dev/null 2>&1 || die "cannot read $config (not valid JSON); re-run \`jailbee apply\` on the host."
[ -n "$profile" ] || profile="$(jq -r '.default_profile' "$config")"
if ! jq -e --arg p "$profile" '.profiles[$p]' "$config" >/dev/null; then
    # One profile per line, as --help lists them: a wrong name is also how
    # someone finds the names. A profile written before max_context_window
    # has no ceiling, so that half is left out rather than invented.
    known="$(jq -r '. as $c | .profiles | to_entries[]
        | "  \(.key)\(if .key == $c.default_profile then " (default)" else "" end): window \(.value.context_window)\(if .value.max_context_window then ", up to \(.value.max_context_window)" else "" end)"' "$config")"
    die "unknown profile '$profile'. Profiles:"$'\n'"$known"
fi

get() { jq -r --arg p "$profile" ".profiles[\$p]$1" "$config"; }
key_file="$(get .key_file)"
[ -r "$key_file" ] || die "cannot read the proxy key $key_file; re-run \`jailbee apply\` on the host."
key="$(tr -d '\n' < "$key_file")"
[ -n "$key" ] || die "proxy key $key_file is empty; re-run \`jailbee apply\` on the host."

unset ANTHROPIC_API_KEY ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL \
       ANTHROPIC_DEFAULT_FABLE_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL \
       ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL
export ANTHROPIC_BASE_URL="$(get .base_url)"
export ANTHROPIC_AUTH_TOKEN="$key"

# The session's window: the profile's default, or --context up to the profile's
# ceiling. A litellm.json from before `max_context_window` has no ceiling, so
# only its default is allowed until `jailbee apply` rewrites it.
default_window="$(get .context_window)"
ceiling="$(get '.max_context_window // empty')"
window="$default_window"
if [ "$have_context" -eq 1 ]; then
    lower="${context,,}"
    if [ "$lower" = default ]; then
        window="$default_window"
    elif [ "$lower" = max ]; then
        window="${ceiling:-$default_window}"
    elif [[ "$lower" =~ ^([1-9][0-9]{0,11})([km]?)$ ]]; then
        window="${BASH_REMATCH[1]}"
        case "${BASH_REMATCH[2]}" in
            k) window=$((window * 1000)) ;;
            m) window=$((window * 1000000)) ;;
        esac
    else
        die "--context '$context' is not a size: use 272k, 1m, a token count, max or default"
    fi
    if [ "$window" -gt "${ceiling:-$default_window}" ]; then
        hint=""
        if [ -z "$ceiling" ]; then hint=" (if this profile allows more, re-run \`jailbee apply\` on the host)"; fi
        die "--context $context is $window tokens, above the ${ceiling:-$default_window}-token ceiling of profile '$profile'$hint"
    fi
    if [ "$window" -gt "$default_window" ]; then
        printf 'claude-jb: %s-token context window (default %s); a larger window costs more per request.\n' \
            "$window" "$default_window" >&2
    fi
fi
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="$window"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
for tier in fable opus sonnet haiku; do
    model="$(get ".tiers.$tier // empty")"
    if [ -n "$model" ]; then
        export "ANTHROPIC_DEFAULT_${tier^^}_MODEL=$model"
    fi
done

# The profile's model-policy text. With text, the user's own append flags are
# merged after it (one argument; Claude Code's handling of a repeated flag is
# undocumented); without, `args` already carries them verbatim.
instructions="$(get '.instructions // empty')"
if [ -n "$instructions" ]; then
    combined="$instructions"
    for part in "${append_parts[@]}"; do
        if [ "${part:0:1}" = f ]; then
            text="$(cat -- "${part:1}")" || die "cannot read ${part:1} (--append-system-prompt-file)"
        else
            text="${part:1}"
        fi
        if [ -n "$text" ]; then combined+=$'\n\n'"$text"; fi
    done
    [ "$(printf '%s' "$combined" | wc -c)" -le 131071 ] \
        || die "the profile's instructions plus --append-system-prompt exceed the 128 KiB limit on one argument"
    args=(--append-system-prompt "$combined" "${plain[@]}")
fi

effort="$(get '.effort // empty')"
if [ -n "$effort" ] && [ "$user_effort" -eq 0 ]; then
    args=(--effort "$effort" "${args[@]}")
fi
# Check only the TCP listener, not authentication. Bound the probe so a
# stopped proxy fails with guidance rather than hanging or opening Claude.
if [ "${JAILBEE_LITELLM_SKIP_REACHABILITY:-}" != "1" ]; then
    [[ "$ANTHROPIC_BASE_URL" =~ ^http://([0-9.]+):([0-9]+)$ ]] \
        || die "invalid proxy address; on the host run \`jailbee litellm up\` and \`jailbee apply\`."
    host="${BASH_REMATCH[1]}"; port="${BASH_REMATCH[2]}"
    timeout 3 bash -c 'exec 3<>/dev/tcp/$1/$2' _ "$host" "$port" 2>/dev/null \
        || die "proxy $host:$port is unreachable; on the host run \`jailbee litellm up\` and \`jailbee apply\`."
fi
exec claude "${args[@]}"
EOF
chmod 0755 /usr/local/bin/claude-jb

# jailbee-inbox: wait for, list and read the messages the orchestrator (or
# the host broker) delivers to this container's inbox
# (${JAILBEE_ORCHESTRATION_DIR:-~/.jailbee/orchestration}/inbox/*.json).
# Unread = a regular, non-hidden inbox/*.json; read ones move to inbox/read/.
# inotify-tools makes `wait` event-driven; without it `wait` polls.
install -m 0755 "$JAILBEE_PROVISION_DIR/jailbee-inbox" /usr/local/bin/jailbee-inbox

# Passwordless sudo for the dev user.
echo "${CONTAINER_USER} ALL=(ALL) NOPASSWD:ALL" \
    > "/etc/sudoers.d/90-${CONTAINER_USER}"
chmod 0440 "/etc/sudoers.d/90-${CONTAINER_USER}"

# Make ssh-agent → gpg-agent forwarding survive login shells that lose
# the environment (a plain `sudo -i`, an SSH login into the container).
# The base Incus profile sets SSH_AUTH_SOCK at exec level, but env_reset
# clears it — so ssh-add reports "no identities" even though the
# gpg-agent socket is mounted in. The profile.d snippet re-derives it
# from XDG_RUNTIME_DIR, which PAM keeps set.
#
# Two guards, because one golden image serves every config:
#   - Only when SSH_AUTH_SOCK is still unset. jailbee's own `jailbee
#     shell` / `jailbee exec` reach the shell via `incus exec --user` +
#     setpriv, which preserves the profile value, and `container.env` is
#     documented to be able to point SSH_AUTH_SOCK at a different agent —
#     neither may be overwritten here.
#   - Only when the socket actually exists. With `gpg.enabled: false`
#     jailbee attaches no gpg-socket device and the host may run no
#     gpg-agent at all; exporting a path to a missing socket breaks
#     ssh-add and shadows any agent started inside the container.
#
# The sudoers drop-in additionally preserves SSH_AUTH_SOCK plus the GUI
# socket env for callers that already have it set (e.g. `jailbee ide`).
echo "==> Configuring SSH_AUTH_SOCK passthrough for login shells / sudo"
cat > /etc/profile.d/jailbee-env.sh <<'EOF'
if [ -z "${SSH_AUTH_SOCK:-}" ]; then
    _jailbee_gpg_sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/gnupg/S.gpg-agent.ssh"
    if [ -S "$_jailbee_gpg_sock" ]; then
        export SSH_AUTH_SOCK="$_jailbee_gpg_sock"
    fi
    unset _jailbee_gpg_sock
fi

# Claude Code resolves its config home as `CLAUDE_CONFIG_DIR || $HOME/.claude`
# and its global config as `(CLAUDE_CONFIG_DIR || $HOME)/.claude.json`. Setting
# the variable to the value the default already resolves to therefore changes
# exactly one thing: the global config file moves from $HOME/.claude.json into
# $HOME/.claude/, which is JailBee's shared directory mount. That retires the
# file-level bind for .claude.json, whose inode any atomic rewrite on the host
# would replace, leaving the container bound to the old one.
# Not forced: `container.env` can override it.
if [ -z "${CLAUDE_CONFIG_DIR:-}" ]; then
    export CLAUDE_CONFIG_DIR="$HOME/.claude"
fi
EOF
chmod 0644 /etc/profile.d/jailbee-env.sh

cat > /etc/sudoers.d/90-jailbee-env <<'EOF'
Defaults env_keep += "SSH_AUTH_SOCK WAYLAND_DISPLAY DISPLAY"
EOF
chmod 0440 /etc/sudoers.d/90-jailbee-env

# Pre-create dev-owned parent dirs for bind-mount targets. Without these,
# Incus auto-creates parents as root:root at container start, which blocks
# the dev user from later writing siblings into them — e.g. `docker buildx`
# into /home/dev/.docker/buildx, or `uv` into /home/dev/.config/uv when
# /home/dev/.config/JetBrains is bind-mounted in.
#
# Run `mkdir -p` as ${CONTAINER_USER} so EVERY intermediate (e.g. `.local`,
# `.local/share`, `.java`) is dev-owned. The earlier "mkdir as root + chown
# only the leaf" approach left intermediates root:root, which broke tools
# writing siblings of the precreated leaves — e.g. the jailbee-new-time
# ensure-claude.sh step's `mkdir /home/dev/.local/share/claude` (EACCES).
#
# `.cache` is here because a repo may bind-mount a shared cache under it
# (e.g. `~/.cache/uv`), which makes Incus auto-create `.cache` as root:root.
# The Claude Code native installer then fails `mkdir ~/.cache/claude`
# (EACCES), leaving an empty version store and no `claude` binary.
echo "==> Pre-creating bind-mount parent dirs owned by ${CONTAINER_USER}"
for d in \
    .cache \
    .docker \
    .config \
    .java/.userPrefs \
    .local/share/pnpm \
; do
    runuser -u "${CONTAINER_USER}" -- mkdir -p "${JAILBEE_USER_HOME}/${d}"
done

# Enable systemd-logind "linger" for the dev user. Without this, the
# per-user runtime dir (/run/user/<UID>) is only created when a real
# PAM login happens — and `incus exec` bypasses PAM. With linger on,
# logind creates /run/user/<UID> with the right owner+mode at container
# boot, *before* Incus mounts disk devices like the Wayland socket.
# This avoids the race where Incus would otherwise auto-create the
# parent as root:root, mode 700, leaving the bind-mounted sockets
# inaccessible to the dev user.
echo "==> Enabling systemd-logind linger for ${CONTAINER_USER}"
mkdir -p /var/lib/systemd/linger
touch "/var/lib/systemd/linger/${CONTAINER_USER}"

# Keep the user manager that linger just guaranteed away from the host's
# sockets. jailbee bind-mounts the host's own /run/user/<uid>/gnupg and
# /run/user/<uid>/pulse *directories* into the container, and these user
# socket units listen on paths inside them — unlinking whatever file is
# already there before they bind. Left alone, a container boot therefore
# deletes the host's live agent sockets: the host gpg-agent logs "socket file
# has been removed - shutting down", and the container's own agent, which has
# no smartcard access, answers in its place. The host's YubiKey vanishes from
# `ssh-add -l` mid-session because a container restarted.
#
# `--global` (i.e. /etc/systemd/user/) because these are per-user units and
# the dev user's session is created at boot, not by this script. Masked rather
# than disabled so an `apt-get install` of gnupg or pipewire inside the
# container cannot quietly re-enable them.
#
# The read-only flag on those two devices (jailbee's runtime_mounts) is the
# actual guarantee — it also covers gpg's own agent autostart, which never
# goes through systemd. This half keeps the container from even trying, so no
# failed units pile up in the user session.
echo "==> Masking user socket units that would clobber the host's sockets"
systemctl --global mask \
    gpg-agent.socket gpg-agent-ssh.socket \
    gpg-agent-extra.socket gpg-agent-browser.socket \
    dirmngr.socket keyboxd.socket \
    pulseaudio.socket pipewire-pulse.socket 2>/dev/null || true

# Run user/repo install.d snippets (and after this task, the bundled
# feature snippets too). Empty files are skipped — that's the same-name
# shadow disable mechanism.
if [ -d /provision/install.d ]; then
    for f in /provision/install.d/*.sh; do
        [ -e "$f" ] || continue
        [ -s "$f" ] || continue
        echo "==> Running install.d snippet: $(basename "$f")"
        bash "$f"
    done
fi

echo "==> Running plumbing smoke checks"
git --version
tmux -V

echo "==> Cleaning up apt caches"
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "==> Disabling SSH server (enable manually if needed)"
systemctl disable ssh || true

echo "==> Provisioning complete"
