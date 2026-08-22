# The workflow-video recording rig

Three scripts that turn this dev container into a machine that can record the
website's workflow videos. They drive jailbee against the **nested** Incus
daemon `.jailbee/install.d/75-incus.sh` bakes into the golden image, so
recording needs no host session and a retake costs seconds.

```bash
./substrate.sh       # the repo the videos are recorded against (run this first)
./up.sh              # environment: daemon, bridge, profiles, golden image
./seed-claude.sh     # an authenticated agent inside the containers
cd .. && ./render.sh --record a   # record video A
```

**[`../README.md`](../README.md) is the runbook** — the per-take checklist, the
cut lists, the rules that cost failed renders, and the honesty rules. This file
covers only what `up.sh` does to the container and why.

## What `up.sh` does, and why each step is there

Only two of its eight steps are things you would expect to run on a real host.
The other six exist because a feasibility gate failed without them on
2026-08-21, and none of the six is documented anywhere else in this repo. The
evidence for each is in the design spec's section 13.

| Step | Why |
|---|---|
| start `incus` | the unit ships disabled — most containers do not want a second daemon running |
| mask `/dev/dri` | `profiles.py` puts a `mode` property on every dri device, and Incus refuses `mode` when the parent is a nested container. Masking the directory means no dri device is generated at all |
| disable the registry mirror | it defaults to on, and `jailbee init` hard-fails on a missing mirror container before doing anything else. The substrate pulls no Docker images |
| `root:<uid>:1` in `/etc/subuid` | the base profile's `raw.idmap` asks for an identity mapping that `newuidmap` refuses unless the file covers that uid |
| `chmod 0755 /opt/google-chrome` | a 0750 extraction root hides Chrome from the container user and VHS then hangs with no error. Fixed at the source in `a1b4e47`; this line is only needed until the image is rebuilt |
| create `incusbr0` | jailbee hardcodes the name but never creates it — on a host `incus admin init` already did. **The CIDR must be explicit**: `ipv4.address=auto` hangs indefinitely here |
| `jailbee init` / `apply` | profiles, ACL, shared dirs |
| `jailbee base build` | the substrate's golden image, ~3m18s and ~871MiB |

Measured: 4m32s cold, 1.3s when there is nothing to do.

## The two steps that do not survive a restart

The `/dev/dri` mask and the Chrome chmod are runtime state. After the container
restarts, **re-run `up.sh`** — it is idempotent, so this costs a second.

The symptom of forgetting is worth recognising, because neither failure names
its cause: `jailbee apply` fails on the base profile with *"The `mode`
property may not be set when adding a device to a nested container"*, and — if
apply died before reaching the binds profile — the agent in a fresh container
reports **"Not logged in"** even though the credentials are seeded correctly.
One root cause, two symptoms that look unrelated.

## Why the mask instead of a code fix

`incus profile device unset <prefix>-base dri-renderD128 mode` also works, and
it is the wrong answer: `jailbee apply` regenerates the profile from config and
puts the property straight back. Masking the directory changes what
`profiles.py` can see, so it survives every later apply.

A real fix would live in `profiles.py` (omit `mode` when the daemon is nested)
and would let a nested host work with no rig step at all. That is a product
decision about whether jailbee supports nested hosts, so it stays out of the
rig.

## Tearing down

```bash
cd ../../../.local/video-rig/jailbee-demo
jailbee ls -o json | jq -r '.[].name' | xargs -rn1 jailbee destroy --force
incus image delete jailbee-demo-base
incus network delete incusbr0        # only if nothing else uses it
sudo umount /dev/dri                 # restores GPU passthrough for new containers
```

The profiles and the ACL can stay: they are inert without containers, and
`up.sh` reuses them.

The substrate itself (`.local/video-rig/jailbee-demo` and its bare origin) is
gitignored scratch: delete both and `./substrate.sh` rebuilds them from
`../substrate/`, which is committed.
