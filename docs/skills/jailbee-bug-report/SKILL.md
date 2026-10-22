---
name: jailbee-bug-report
description: Use when JailBee itself misbehaves — a `jb`/`jailbee` command crashes, prints a traceback, does the wrong thing, or the container, egress, mounts, git bridge or dashboard act contrary to the documentation — and the user wants it reported, or you have found a defect in JailBee (not in the user's own project). Stages a bug report against the JailBee repository through the issue outbox. Trigger on "report a jailbee bug", "file a jailbee issue", "this is a jailbee bug", "jailbee is broken", "ilmoita jailbee-bugi", "tee bugiraportti jailbeestä", "jailbee ei toimi".
---

# Reporting a JailBee bug from inside a container

You are inside a JailBee container and JailBee itself — not the user's project —
appears to be wrong. A report goes to the JailBee repository:
`github:VRTFinland/jailbee` (<https://github.com/VRTFinland/jailbee/issues>).

Nothing here reaches GitHub by itself. Like every issue action from a
container, the report is staged as a manifest in the issue outbox and a human
publishes it on the host. Read the **jailbee-issue-management** skill for the
outbox rules and `references/manifest-schema.md` there for the exact manifest
format; this skill only says what a JailBee bug report should contain.

## Is it a JailBee bug?

Report only when the defect is in JailBee: a command's behaviour contradicts the
**jailbee-usage** skill or the docs, a traceback comes out of `jailbee`, or the
environment JailBee built (egress, mounts, git bridge, profiles) is wrong. A
failing build or test in the user's own project is **not** a JailBee bug; neither
is a deliberate restriction (a strict-mode egress block is the design — see
**jailbee-usage**). If unsure, say what you saw and ask.

## Ask before you stage anything

Tell the user what you found and ask whether to stage a report. Do not write a
manifest unprompted: the report becomes public text under their name.

## What the report contains

The container has no `jailbee` binary, so you cannot run `jb --version` or
`jb doctor` yourself. Ask the user to run these **on the host** and paste the
output, or leave a clearly marked placeholder line in the body:

- `jb --version`
- `jb doctor` — only the failing or warning lines are needed.

Then write the body in concise technical English (whatever language the chat is
in) with these parts, in order:

1. **What happened** — the command exactly as typed, and the observed output.
2. **What was expected** — and which doc or skill says so, if one does.
3. **How to reproduce** — the shortest steps from a fresh container; note the
   network mode (`strict`/`loose`) and whether the repo uses submodules or the
   work network if that could matter.
4. **Environment** — JailBee version, Incus version and storage pool driver if
   known, host OS.
5. **Suspected cause** — only when you traced it to a file or symbol; say so
   plainly when it is a guess.

Search first: `gh issue list --repo VRTFinland/jailbee --search "<keywords>"
--json number,title,state` is a read and works here. If the bug is already
reported, stage a `comment` on that issue (with the new detail) instead of a
second issue.

## Leave out

Tokens, credentials, `.claude.json` or other account files, full environment
dumps, and paths or hostnames that identify the user's private repository or
network. Replace them with placeholders. Trim logs to the lines that matter.

## The manifest

Use `repo: "github:VRTFinland/jailbee"` — the named exception for a repository
outside the current project. The human sees a foreign-repository warning and
confirms it separately. Leave `labels` empty: GitHub drops labels from users
without triage rights. A `create` action needs a local `ref`.

```json
{
  "version": 1,
  "actions": [
    {
      "type": "create",
      "repo": "github:VRTFinland/jailbee",
      "ref": "new-bug",
      "title": "<command>: <what goes wrong>",
      "body_file": "001-jailbee-bug.md",
      "labels": []
    }
  ]
}
```

Write it to `~/.jailbee/issue-outbox/001-jailbee-bug.json` and the body to
`~/.jailbee/issue-outbox/001-jailbee-bug.md`. Keep the title short and
specific; the body carries the detail.

## When you're done

Tell the user, verbatim: run `jb issue apply <container>` on the host —
**nothing has been published yet.** They can inspect first with
`jb outbox show <container> issue/001-jailbee-bug.json`. Never run a host apply
or `gh issue create` yourself.
