# JailBee documentation

`jailbee` (short form `jb`) runs isolated, per-branch development environments
in Incus system containers. Every git branch gets a full container of its own —
its own services, Docker daemon, IP and optionally its own IDE and browser on
your desktop.

New here? [Installation](installation.md) sets up the host, then
[Getting started](getting-started.md) walks the first container end to end.

## Setup

| Doc | What's inside |
|---|---|
| [Installation](installation.md) | One-time host setup: Incus, UID delegation, installing the CLI (plus conditional firewall / kernel-keyring steps) |
| [Getting started](getting-started.md) | Concepts, configure a repo, build the image, and a "typical day" walkthrough |
| [Using JailBee from a Mac](macos.md) | A Mac as client of a Linux host, or of a Linux VM on the Mac (experimental): SSH service plus Windows App for GUI apps |

## Daily use

| Doc | What's inside |
|---|---|
| [FAQ](faq.md) | Short answers to the common questions, each linking to the page that covers it in full |
| [Commands](commands.md) | Full command + flag reference table |
| [Git bridge and branch workflows](git-bridge.md) | Host↔container git bridge, stacked PRs, mount vs clone, PR review, `gh` inside containers |
| [Setting up JailBee in your own project](project-config.md) | Tutorial for adapting JailBee to your own repo and stack |
| [Claude Code through LiteLLM](litellm.md) | `claude-jb` and `jailbee litellm`: Claude Code on a ChatGPT subscription or another provider's models |
| [Remote GUI over SSH](remote-gui.md) | The shared RDP display for GUI apps launched from a remote SSH session |
| [Troubleshooting](troubleshooting.md) | Common failures by symptom, and how to remove JailBee |

## Reference

| Doc | What's inside |
|---|---|
| [Configuration reference](config.md) | Every `.jailbee/config.yaml`, `global.yaml` and host-local `repos/<prefix>.yaml` key |
| [Generic agent support](agents.md) | Wiring a terminal coding agent (Claude Code or otherwise) into the container lifecycle; the shipped presets and their verification status |
| [Security and limitations](security.md) | Isolation model, git-remote handling, known limits |
| [Architecture](architecture.md) | How the pieces fit together |
| [Who JailBee is for](comparison.md) | What JailBee is good at, what it costs, and how it differs from Dev Containers, BranchBox, nono and Docker Sandboxes |

## Project internals

These live in the repository rather than on this site — they are maintainer
procedure, not user documentation.

| Doc | What's inside |
|---|---|
| [Manual testing](https://github.com/VRTFinland/jailbee/blob/main/docs/manual-testing.md) | End-to-end smoke-test recipes (require a real Incus daemon) |
| [Releasing](https://github.com/VRTFinland/jailbee/blob/main/docs/releasing.md) | Release process |
| [Contributing](https://github.com/VRTFinland/jailbee/blob/main/CONTRIBUTING.md) | Development setup and repo conventions |
