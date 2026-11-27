# Web dashboard

The web dashboard is a browser view of the same host state the terminal
dashboard shows: the front page of repositories and containers, per-repo and
per-container session lists, and a readable, live-following transcript of an
agent session. It runs on the host as the current user and comes with every
install. Reading is available by default; messaging a live Claude session
requires an explicit remote permission.

Which container ran a session is recorded by the shared state service, which
runs only while some dashboard (terminal, Qt or web) is connected. A session
it never saw (one that started and ended with no dashboard open, or predates
the recorder) is attributed from its own transcript instead: the directory and
git branch it started in, and when. It belongs to a container whose branch
label (the branch the container was created on) is that branch, whose
repository directory holds that directory, and which already existed when the
session started; when several qualify (two containers on one branch) the
session stays unattributed rather than guessed. So a container recreated under the same name does not inherit
the older one's sessions. A recorded attribution always wins over this one. A
mount-mode container has no branch label and lists only recorded sessions, and
a session started after the branch was renamed inside its container names the
new branch and stays unattributed unless it was recorded.

Session titles use the first typed prompt, skipping agent wrapper records,
local command output, caveats and injected context. A session with only slash
commands uses its first command (for example, `/model`) instead of raw markup.
Once a command fallback is available, title lookup stops after 32 candidate user
records rather than parsing a command-only transcript to the end, and it never
reads past a transcript's first 4 MiB. Task
notifications, `!`-mode input/output and prompt hooks are agent metadata too.
A leading wrapper followed by typed text is split in the transcript: the wrapper
is shown as metadata, and only the typed text is shown as the prompt. Unknown
XML-like tags, such as `<example>`, remain part of the typed prompt. The summary
Claude Code writes after `/compact` and its "[Request interrupted by user]"
notices are metadata too: they are shown as context and never become a title.

Session lists stay fast on large transcripts. A list row reads only the
opening of a transcript (title and start time) and its last 256 KiB (last
activity and recent model), so a model used only in the middle of a long
session is not named in the list; the transcript itself shows every entry's
model. What was read is kept in the state database (`session_info_cache`),
keyed by the file's path, size and modification time, so a restarted or
upgraded server rereads only transcripts that changed. A listing loads only
the stored rows of the project directories it lists. The server also reads
every visible repository's sessions in the background when it starts, after
dropping the stored rows of deleted transcripts; no request waits for it.

Container and repository session lists default to **Live only**. The visible
switch changes to **All sessions** for this visit, revealing earlier sessions
(and the container's history search and pager). With no live sessions, **Show all
sessions** reveals the history in one tap. **Settings > Sessions** remembers the
default through **Show only live sessions** and **Open the only live session
directly**, both on by default. With the filter and shortcut enabled, opening a
list with exactly one live session opens that transcript directly, replacing the
list in browser history so Back returns to the entry page. Returning from that
transcript to its list never jumps back; reloading the list starts a new visit.

A container page shows its live sessions as soon as they are known (they come
from the state service's snapshot) and keeps them at the top, independently of
history paging or search. Earlier sessions are recorded and attributed, newest
first, and fetched separately in pages of 20. **Previous** and **Next** buttons
work in both normal and e-ink display profiles; there is no infinite scroll.
If history shrinks while browsing, the list returns to and fetches the last valid page.
**Search earlier sessions** searches titles (the first prompt) and session IDs
on the server after a short typing pause, resets to the first page, and shows
**No matches** for an empty result. Clearing it restores the history. The history
endpoint `/api/containers/{name}/sessions/history` accepts `limit` (1–100,
default 20), `offset` (default 0) and `q` (case-insensitive substring); the response
includes the matching `total`, `limit` and `offset` alongside the sessions.
A repository lists only the sessions started in
its own containers' checkout (each container's repository directory, or below
it): repositories that share one agent config home by mistake do not list each
other's sessions. Claude names a session's project directory after its working
directory with every character but letters and digits turned into `-`, so
the directory name cannot tell `/home/dev/app-api` from a directory `api` below
`/home/dev/app`; the working directory recorded in the session itself decides,
and a repository `app` does not list `app-api`'s sessions.

Rows on the front page are styled like the terminal dashboard's: a container
booting, in autostart, running another background job or in a failed job gets a
coloured edge and a phase label, and a Stopped container is dimmed. The e-ink
theme has no colour, so phases differ by edge style (dashed, dotted, double,
heavy) and the label, and a Stopped row is drawn without top, right and bottom edges instead of a fade.

The right-edge **⋮** (Quick settings) menu filters front-page and repository container lists
by **All**, **Running** (Incus state Running), or **Active** (a non-grey
agent marker in the terminal dashboard: busy, waiting, shell, API errors, or
idle with a real event less than 30 minutes old). Recent-idle membership is
independent of session count or cell width; a bold `idle 5m` label explains its
freshness in both normal and e-ink profiles. The server refreshes ages and filter
membership as time passes, even without a new state snapshot. Tooltips and accessible labels
name each button; the selected button stays pressed. The choice is remembered
in this browser and follows live snapshot updates (the e-ink display profile
keeps its normal slow/on-demand refresh). Repositories with no retained rows
are hidden while filtering. A dot on the menu icon and a slim **Showing: Active · show all**
notice keep a non-All filter visible; **show all** resets it. All has no notice.
Nonmatching listed ancestors remain dimmed as context above matching children,
so the tree stays intact. Tree prefixes are recomputed for the retained rows.
This does not change the terminal dashboard's list.

Children in the same repository appear indented under their orchestrator on
the front page and in repository container lists. Child counts and container
pages include children across all visible repositories; excluded repositories
never contribute to counts. Each child shows its state and runner when known.
A `Child of` label links to the orchestrator even across repositories;
`orchestrator not listed` means it is absent from the entire visible snapshot,
not just the current repository. An orchestrator's repository and short name
come from its snapshot row, with prefix matching only when that row is absent.
An orchestrator in an excluded repository is labelled `hidden orchestrator`,
without its name or a link.

Container pages link to their children or back to their orchestrator, resetting
the page turn and scroll position when following a related-container link.
These relationships update with the live snapshot. Indentation and the
`Child of` label distinguish nesting without colour; phase edge styles remain
reserved for booting, autostart, background jobs and failures, also on nested
rows in the e-ink theme.

## Message the agent

Messaging is on by default. To disable it, set this in the host-global
configuration and restart the web server:

```yaml
remote:
  web:
    agent_chat: false
```

This right is independent of command-running rights and
applies to every logged-in device. With login disabled, anyone who can reach the
server has it too. Repository visibility still applies: an excluded repository's
sessions cannot be messaged.

The transcript reader's **Message agent** field sends multi-line text to the
session being read, using the same boundary as
[`jailbee agent say`](commands.md). The message is pasted into that
Claude session's tmux pane as an ordinary user turn; Claude queues it as the
next turn while working and replaces its ghost suggestion with the pasted text.
If real text is already typed in the terminal, the paste joins that text.
There is no web marker in the transcript. Only Claude is supported, including
`claude-jb`; subagent transcripts cannot be messaged.

In the composer, Enter sends and Shift+Enter adds a line; on phones, use
**Insert newline** to add a line, and the keyboard's Enter key is labelled Send.
While an input method is composing text, Enter does not send. In an answer
panel's free-text field, Enter advances to the next question, then to the summary;
it never sends. Send numbered answers only from the summary's **Send** action.
When permission is off, the composer and answer-panel send controls are hidden;
questions remain readable in the transcript. The field is disabled with a reason
when the session is no longer live, the agent is unsupported, or its pane cannot
be found. The only agent-state refusal is a nonempty `waitingFor` in Claude's own
session record: permission prompts (`permission prompt`), plan and other dialogs
(`dialog open`), questions/MCP elicitation (`input needed`), `worker request`,
`sandbox request`, or `goal proposal` in Claude Code 2.1.332. Unattended mode removes
the AskUserQuestion and EnterPlanMode tools, but user-entered plan mode and other
dialogs can still occur. No screen contents, busy state, ghost text or typed draft
are used to block sending. A reported dialog returns `waiting_at_dialog` and the
message **The agent is waiting at a dialog in the terminal; answer it there
first.** No text is pasted and Enter is not sent; the CLI exits nonzero too.
Every refusal keeps your text and shows the reason in the reserved status row,
which also announces delivery updates. A fresh send result
takes precedence over an availability reason for five seconds. Avoid
typing into the same pane while sending: the paste cannot lock out concurrent
terminal typing.

The composer starts collapsed on mobile widths and expanded on desktop. Use
**Message agent** to expand it and focus the field, or hide it without losing its
draft. An explicit show/hide choice is remembered on this device when browser
storage is available. The composer is hidden while the answer panel is open and
returns in its remembered state when the panel is hidden or answered. Send status
remains visible when the composer is collapsed.

**Waiting for transcript** means the text was pasted; **Delivered** appears only
when a matching ordinary user entry arrives in the live transcript. The field
and send button stay disabled while that confirmation is pending. No separate
acknowledgement or queue is used. Confirmation is bounded: commands such as
`/compact` may not produce an ordinary user entry, so an unconfirmed paste does
not leave messaging locked indefinitely: after 30 seconds it reports
**Pasted; transcript confirmation unavailable** and unlocks. Reloading the transcript or reconnecting
also resets that wait, but an in-flight send still completes and clears its
text or answer set when delivery succeeds. The composer also works in the e-ink display profile,
above its page controls.

A leading `/` invokes a Claude command and `!` invokes its shell mode, just as at
the terminal. Control characters except newline and tab are stripped before
pasting. Concurrent web and CLI sends to one pane are serialized, but terminal
typing cannot be locked out.

`jb remote web auth log` records sends and refusals with the device or token name,
session, outcome and sanitized text in the local state database. Messages have
separate retention and listing budgets so they cannot hide security events. Message text is not
printed to the server's audit logger. Treat that database as private transcript
data. `remote.ssh.agent_chat` is a separate, also default-on right for
`jailbee agent say` over SSH.

### Answer numbered questions

When the latest assistant reply contains well-formed numbered ❓ questions, an
**Answer** bar offers them as a non-modal panel. On a phone, expand it to half or
full height; on a wide screen it sits beside the transcript. The caret points in
the direction the panel will move. Use **Hide** to read on without losing answers;
discarding a draft is a separate action. The conversation remains scrollable, and e-ink uses
hairlines without animation. Long question bodies and summaries use the panel's
Previous/Next page controls in the e-ink profile rather than swipe scrolling.

The panel shows one question at a time. Recommended choices are preselected and
badged; choose another full-width option or add a note. A question without options
has a text field. The compact title row contains the question number, title, body,
step dots and controls. Truncated text can be shown in full on demand. Use Back,
Next or the step dots to move between questions; the **Show in conversation**
icon finds and highlights the question in the transcript. The footer places **Discard answers** on the left and **Next** on the right;
free-text Enter advances through the questions to the summary, where **Send** is
the only send action. Discarding requires inline confirmation with **Keep
answers** or **Confirm discard answers**. Answer send status and warnings use the
same reserved row as the composer. The Hide control uses a distinct minimize icon.
The last step lists every answer, marking nonempty answers to questions with a
recommendation as recommendation or changed. Select a summary
row to edit it. Only this summary offers Send, beside a preview of the exact
message: one `Q12: a` line per question, with an optional ` – note`.

Editing an answer creates a draft in this browser, keyed by session and source
message. Untouched recommendations follow the newest questions automatically.
Closing the panel or reloading preserves edited drafts when browser storage is
available. The browser retains at most 100 drafts and completion history for 100
sessions, with at most 100 dismissed question sets per session. They do
not sync between devices, and blocked storage falls back to an in-memory draft.
A newer assistant reply leaves the older draft attached to its source with a
notice; send it deliberately or discard it. Questions in the new reply get their
own set. Select a question set to return to its own answers; selecting a set
waiting in the e-ink new-message buffer reveals its source explicitly. Discard
removes the saved draft, and sent or discarded sets stay dismissed after reload.
Older questions without a retained
draft can always be answered by number in **Message agent**.

Malformed question blocks do not open a panel. The free-text composer remains
available, and a refused answer send keeps the draft and shows the reason just
like a free-text message.

## Install

The web dashboard comes with every JailBee install: its dependencies are part
of the package, and released wheels carry the built browser app. The old
`jailbee[web]` extra still installs, and adds nothing.

From a source checkout the browser app must be built (needs Node.js 22.22+ or
24.15+, and npm) before the package is installed. `make install` builds it
when npm is available, and `make install-web` fails without npm instead:

```bash
make install-web                    # builds frontend/, then installs
make web && uv sync                 # editable checkout: build once, in place
```

Without the built app, `jb remote web serve` exits with a message saying so.

## Testing from a checkout

Run `make web-test` for the browser app's Vitest suite and `make web-e2e` to
build the app and exercise the real server in the system Chrome. The latter
installs the separate `e2e` dependency group; it needs Node.js, npm and Chrome,
but no Incus or live agent. Browser tests stay outside the ordinary pytest
suite. Failed browser tests retain Playwright traces and screenshots under
`_build/e2e-results/`. The suite also compares the `/ui` gallery against committed
screenshots. For intended design changes, run `make web-e2e-snapshots`, review
the PNG diff and commit the references; see "Gallery screenshot references"
in `tests_e2e/README.md` of your checkout.

Before changing the browser app's look, read `frontend/DESIGN.md` in your
checkout: which UI primitive to use, the design token families, the e-ink
rules, and how to add a primitive.

The **Web dashboard** GitHub Actions workflow runs both targets for pull
requests and pushes to `main` or `dev` that change the frontend, web server,
transcript reader, browser tests or their shared fixtures, dependency
manifests, Makefile or workflow. It can also be started with
`workflow_dispatch`. It uses the runner's Chrome and uploads browser failure
evidence as the `web-e2e-failure` artifact. Unrelated changes do not trigger
this workflow; the ordinary CI gate stays separate.

### Sweeping your own sessions

`make web-e2e-sweep CLAUDE_HOME=<path>` (default `~/.claude`) renders every
session and subagent of a Claude home through the same test server and Chrome,
and writes a Markdown report under `.local/web-e2e-sweep/`. It reads the home
only, commits nothing and never runs in CI. The report starts with a summary
line and the unknown entry types across the home, then lists each session with
a finding: load errors, JS errors, CSP violations, renders slower than
`SWEEP_SLOW_MS` (default 2000) and entry types the transcript reader neither
renders nor knowingly skips, each with its first line in the file. Clean
sessions are only counted. Unlike the browser tests, the sweep records browser
errors in the report (`BrowserErrors.drain`) instead of failing on them. The
Makefile's comment on the target describes the
format in full.

### Writing browser tests

Each test's `server` has a fresh database and rate limits. `logged_in` performs
a real PIN login against that test's server (including a fixture override),
so logout and revocation cannot invalidate another test's token.

The security-key tests use Chrome's virtual WebAuthn authenticator, with no
physical key needed. `make_server(setup=True)` serves the real registration-only
`create_setup_app(SetupDeps)`: the dashboard itself has no setup API. Stopping
that server and calling `start()` serves `create_app(WebDeps)` at the same
origin over the same database. Tests register at `http://localhost` (a secure
WebAuthn context with a domain RP ID), log in with the key, and check that a
removed key is visibly refused. Console, page and CSP checks stay enabled.

The automatic `browser_errors` collector watches all pages and popups in
function-scoped contexts created through the synchronous Playwright API:
`browser.new_context()` and `browser.new_page()`, including on a second
launched browser. Close these pages/contexts in the test or its function-scoped
fixture teardown; closing them first settles pending reports. Creation from
module/session-scoped fixtures fails loudly rather than sharing a collector
between tests. `launch_persistent_context()` is rejected before launch; use
`browser.new_context()` instead. `page.set_content()` is also rejected: it
replaces the document without running init scripts and removes CSP listeners;
use `page.goto()` to navigate. The harness supports the sync API, not async
Playwright or direct Playwright internals.

It checks at the end of the test body and again at teardown after settling
pending browser events. Declare only intentional errors, by category, for
example:

```python
browser_errors.expect("console", "intentional message")
browser_errors.expect("page", re.compile(r"Error: exact message"), times=2)
browser_errors.expect("csp", "script-src", at_least=1)
```

Strings match literal substrings; compiled regexes must match the whole
message. Each declaration consumes one distinct hit by default; `times` sets
an exact count, while `at_least` sets a minimum with no upper limit. Overlapping
patterns are matched jointly, not greedily in declaration or arrival order.
Extra hits and unmet expectations fail the test.

The restart test in `tests_e2e/test_front_page_live.py` deliberately waits for a
refused WebSocket reconnect. It
allows only `WebSocket connection to 'ws://127.0.0.1:<server-port>/api/ws'
failed: Error in connection establishment: net::ERR_CONNECTION_REFUSED`,
matched as a whole message for that server's actual port. No other connection
failure shapes are excused, and ordinary tests do not suppress reconnect errors.
A failed stop is reported directly; the test starts the server again only
once stopping and observing the expected reconnect have succeeded.

`tests_e2e/test_sessions.py` exercises the hand-written fixture sessions at
both desktop and phone widths: repository/container lists, Markdown and code,
folded tools, subagent navigation, thinking settings, live appends and
restart catch-up without lost or duplicated replies. Reuse
`tests_e2e.sessions.open_session(page)` from a session list and
`append_entry(server, path, text)` to write a fixture entry and fire its fake
watch. Copy the fixture home to a temporary directory before appending; with
the server stopped the helper only writes, leaving reconnect to catch up.

`tests_e2e/test_display.py` covers visible themes and font sizes after a reload,
e-ink page turns, held transcript messages and manual snapshot refresh. It also
walks the front page, repository, container, transcript and settings at phone
width in both display profiles, and checks the linked PWA manifest and decoded
icons through the real server.

`test_front_page_live.py` covers snapshot additions, changes and removals without
navigation; phase labels and computed monochrome edges in the e-ink theme;
the connecting notice during downtime; and current state after a real server
restart. Its protocol-upgrade test forwards traffic to the real server but
rewrites the reconnect's auth version to simulate an outdated tab. The real
server's 4002 rejection must trigger one document reload and a fresh connection,
then render a subsequent state push before the reload count is checked.
`test_protocol_reload.py` keeps the client stale across that reload: a tab
reloads automatically at most once within a minute, then shows
"This page is out of date; reload" with a manual Reload button. If session
storage is unavailable, it offers manual reload immediately rather than risk
a loop. A connection that authenticates ends that window, so a later
server upgrade reloads again; the banner sits above a view's sticky header,
never over it.

`tests_e2e/test_corpus.py` walks the recorded corpus
(`tests/fixtures/transcripts/claude-corpus`, re-recorded with `make web-corpus`)
as the agent home of repository `toy`: every session opens from the list, every
tool call folds and expands, every subagent link opens its transcript and
thinking follows the setting. It parametrises over `corpus.json`, so a
re-recorded session is covered without editing the tests, and asserts content
only on the markers the recorder's prompts guarantee. Re-record it when Claude
Code's transcript format changes: `make web-corpus` needs a logged-in `claude`,
spends a little (haiku) quota on a throwaway toy repository, and refuses output
that still holds a host, account or credential. Review the diff before
committing it.

## Run it

```bash
jb serve                            # alias of `jb remote web serve`
jb remote web serve --port 8919     # one-run overrides: --listen, --port
```

`serve` stays in the foreground and prints the URL and a one-time login PIN
to stdout. Under the systemd service that output goes to the user journal, so
anyone who can read that journal (for example members of `systemd-journal` or
`adm`) can read the current PIN; turn PIN login off with
`jb remote web auth pin off` once a TOTP key exists.
The default address is `127.0.0.1:8910`. The flags apply to that run only and
are never written to `global.yaml`.

For a permanent service use the systemd user unit:

```bash
jb remote web enable                # install, enable and start jailbee-web.service
jb remote web status                # unit state and address
jb remote web open                  # open the browser already logged in
jb remote web disable
```

The service exits and is restarted by systemd when the installed JailBee
version changes, so an upgrade needs no manual restart.

`open` creates a one-time link that logs that browser in without a PIN. It is
the easy way to use the dashboard locally without turning authentication off.
The link is valid for 60 seconds and works once, for whoever presents it
first. It reaches the browser on its command line, where other accounts on the
same host can read it, so `open` assumes a single-user host. That is JailBee's
own assumption: driving Incus takes `incus-admin` membership, which is
equivalent to root, so other people with shells on the host are not a
boundary the dashboard defends. On a host shared with untrusted accounts, log
in with a PIN, TOTP or a security key instead.

## Logging in

Every request needs a login unless authentication is disabled (below). Methods
can be combined:

| Method | Set up with | Notes |
|---|---|---|
| PIN | on by default | A new one-time PIN is printed by `serve` (to the journal under systemd). `jb remote web auth pin off` refuses PIN logins; at least one TOTP key must exist first. |
| TOTP | `jb remote web auth totp add [--name NAME]` | Prints the secret and a QR code for an authenticator app. `totp ls` and `totp rm` manage keys. |
| FIDO2 security key or passkey | `jb remote web auth fido2 setup` | See below. `fido2 ls` and `fido2 rm` manage keys. |
| One-time link | `jb remote web open` | Single use, valid 60 seconds; assumes a single-user host (see above). |

A successful login gives the browser a session that slides for 30 days. The
login page asks for nothing but the secret: the server names the session after
the browser's User-Agent ("Firefox on Linux", "Safari on iPhone", "Chrome on
Android", or "Browser" when it recognises none). The name shows in the header,
in `jb remote web auth sessions ls` and in the audit log; rename the current
session under **Settings → This session** (`PUT /api/session`, audited as a
`rename` event, recording the old and new name). Older clients may still send a
`device_name`, which is kept. A name is 1-100 characters after trimming and may
not contain control or format characters (the server answers 422); the CLI also
prints any such character escaped.
PIN and TOTP attempts are limited to five a minute, and after five failures
in a row each further failure doubles the wait before the next attempt:
2 seconds after the fifth, 4 after the sixth, up to 30 minutes. A successful login by any method (PIN, TOTP, security key or link) resets
every method's wait. The waits are global, not per source, so an attacker who
reaches the login can keep both the PIN and TOTP at the 30-minute cap with a
few requests an hour, and the owner's own PIN or TOTP attempts are refused
until that lifts. The ways in that the waits do not touch are a security key,
`jb remote web open` (a localhost link, for example over SSH) and restarting
`serve`, which clears the waits; any successful login lifts the PIN and TOTP
waits at once. Links and security keys are not limited. An attempt during the
wait is refused unchecked with HTTP 429 and a `Retry-After` header; the login
page shows that remaining wait in seconds below a minute, otherwise in minutes
rounded up. It buys no guess, and an attempt
still being checked holds back a parallel one. An
owner who mistypes up to four times waits not at all, a fifth or sixth time a
few seconds. The waits are kept in memory, so a restart of `serve` clears
them; under systemd that happens only on an upgrade or a crash, nothing a
remote client can trigger.

A successful PIN login resets the count, which gives a patient attacker a
fresh run: an owner who logs in by PIN daily gives them about 30 % more
guesses a month than the table below.

Each wrong guess is spent: a wrong PIN is replaced by a new one, and a TOTP
code changes every 30 seconds, so every guess has the same small chance. At
the 30-minute cap an attacker who can reach the login gets about 1,450
guesses a month per method and can expect:

| Method on | One guess hits | Even odds after | Within 30 days |
|---|---|---|---|
| PIN (6 digits) | 1 in 1,000,000 | about 40 years | about 0.15 % |
| TOTP, one key | 3 in 1,000,000 (codes ±30 s accepted) | about 13 years | about 0.44 % |
| PIN and TOTP together | both waits run side by side | about 10 years | about 0.58 % |

Without the backoff, five guesses a minute gave even odds in about 32 days
for TOTP and 96 days for the PIN.

Each extra TOTP key adds another three codes to hit. The PIN is printed anew
after every failed attempt, and failed logins show in `jb remote web auth log`
and the journal, so a guessing run is visible long before it is likely to
succeed, but only if someone looks. For a dashboard reachable from the
internet, log in with FIDO2 security keys and turn the PIN off, or better,
do not expose the server at all and reach it over an SSH tunnel or a VPN.

```bash
jb remote web auth status           # methods on, devices logged in
jb remote web auth sessions ls      # logged-in devices
jb remote web auth sessions revoke ID
jb remote web auth log [-n 50]      # logins, failures, backoffs, logouts, revocations, renames
```

Revoking a session refuses the device's next request and closes its open live
view.

Each lengthening of a PIN or TOTP wait is logged once as a `backoff` entry;
refused attempts during a wait go to the journal only, so a flood of them
cannot churn the audit log. The audit log keeps the newest 1000 entries.
Failed one-time-link and FIDO2
logins, which no attempt limit throttles, are kept apart (the newest 100), so
a flood of them cannot push earlier PIN or TOTP failures out of the log.
Agent-message entries have their own retention budget too, so chat cannot evict
login, revocation or security-key events.

Logins, TOTP secrets and sessions live in the JailBee state database
(`~/.local/state/jailbee/state.sqlite`). Its directory is kept `0700` and the
database `0600` (each JailBee process tightens them when it first opens
the database), so other local users cannot read
the TOTP secrets. This is defence in depth: JailBee assumes a single-user
host (see `open` above).

### FIDO2 setup mode

A security key works only for the domain it was registered at, and the domain
is taken from the browser's `Origin` header. Registration therefore has to
happen through the same HTTPS address you will log in at. `fido2 setup` runs a
temporary server that serves a registration page instead of the dashboard
(stop the dashboard first if it uses that port; `--listen` and `--port`
apply). During setup, open the dashboard's root URL in a fresh browser tab.
An already-open tab switches to the setup view by itself, even with a saved
session or a bookmarked login route: a signed-in page when its live connection
reconnects (the setup server closes it with code 4003), a login page within a
few seconds while visible or when brought back to the foreground. Enter the printed
**setup PIN** on that page and register the key, then press Ctrl-C and start the
dashboard again; the open setup view notices that (it polls within a few
seconds while visible) and moves to the login page without a reload. A server that is not accepting logins is reported as unavailable,
not as a wrong PIN. The setup PIN keeps only the five-a-minute limit, without
the backoff: setup mode runs only while you register a key, so do not leave it
running.

### Authentication disabled

```yaml
# ~/.config/jailbee/global.yaml
remote:
  web:
    auth:
      enabled: false
```

With `auth.enabled: false` the server serves everything without a login. It
refuses to bind a non-loopback address unless
`remote.web.auth.allow_unauthenticated_remote: true` is also set, because then
anyone who can reach the port sees the dashboard. Without that opt-in the
server also answers only requests whose `Host` header is `localhost`, a
loopback address or the `listen` address, so a web page cannot reach the
dashboard by pointing its own domain at `127.0.0.1` (DNS rebinding).
State-changing HTTP requests also reject foreign origins, including when login
is disabled, so a page on another site cannot send agent messages through the
local browser. Non-browser clients without an Origin header remain supported.
Authentication is never
skipped based on where a request comes from; see
[Web dashboard authentication](security.md#web-dashboard-authentication).

## Configuration

`remote.web` lives in the host-global `global.yaml`; the full reference is
[`remote.web`](config.md#remoteweb).

```yaml
remote:
  web:
    listen: 127.0.0.1               # IP address to bind
    port: 8910
    auth:
      enabled: true
      allow_unauthenticated_remote: false
    excluded_repos: []              # repos the web never shows, by container_prefix
```

Everything else under `remote.web` is the shared
[remote policy](security.md#remote-policy). The web starts with no commands
allowed, so the current dashboard only reads.

## Themes and display profile

Open **Quick settings** from the **⋮** menu at the right edge of the title bar on any page.
Body and code font-size steppers apply immediately; the theme button cycles
**Auto / Light / Dark / E-ink**. Auto follows the device's colour scheme, including
changes while the page is open. Theme colours and display profile remain independent.
The front-page and repository menus offer container filters; session and subagent
menus offer **Follow new messages**, **Show thinking**, and **Agent context**.
Container pages have no contextual section. Follow edits the preference for the
current display profile: on by default for normal displays, opt-in for e-ink.

**Settings** opens a drawer on wide screens or a bottom sheet on phones without
leaving the current view. Close it with ×, Escape or browser Back; the transcript
and live follow remain in place. A direct `/settings` link still opens the settings
page. Settings include display profile and the login session's name.
Both surfaces edit one live settings store; display choices need no Save and are
stored in per-device localStorage (in memory when storage is blocked). Existing
container filter preferences are preserved. Escape, the icon, or a click outside
the popover closes it and returns focus to the icon. Escape on a closed Agent
context select also dismisses the menu; an open native select handles Escape first.
Navigation closes the menu without returning focus to the icon. Tab stays inside the open
popover. Short screens split the menu into whole-row pages with up/down buttons
and PageUp/PageDown; e-ink also supports **n** (previous) and **m** (next). There is
no scrolling popover. E-ink uses large touch targets and no shadow.

**Agent context** controls the agent's own wrapper records in a transcript:
task notifications, system reminders (including the ones Claude Code appends to
the end of a tool result; a tag elsewhere in the output is the tool's own text), command output and shell input/output.
*Hidden* (the default) sends none of them, so they cost neither bandwidth nor
page weight. *Collapsed* sends a one-line label (for example "Task
notification" or "Shell: ls") and loads the content from the server only when
you expand it. *Shown* sends and displays everything. A slash command you typed
(`/model`, `/clear`) and an interruption notice ("Request interrupted by user")
always appear as a one-line label, in every mode. Changing
the setting reloads the open transcript. The choice is a `meta=hidden|collapsed|shown`
query parameter on the session and subagent endpoints and a `meta` field on the
live `subscribe` message; a withheld record's content is served by
`GET /api/repos/{prefix}/sessions/{agent}/{session_id}/entries/{entry_id}`
(also under `.../subagents/{agent_id}/`). A whole wrapper record keeps its entry
id; context split off another entry (wrappers before a typed prompt, reminders
after a tool result) is a separate record with the id `<entry id>:meta`. A live
read whose records are all hidden still sends an `append`, with no entries and
the new offset.

The display profile **E-ink** is for e-paper readers and slow-refresh
screens: no animation, content moves in page turns instead of scrolling, live
visible transcript entries are held behind a counted **2 new messages ↓** button until
you tap it, revealing them and moving to the first new answer or the end. **Follow new messages on e-ink** in Settings opts into automatic follow at
the transcript's end, using the pager's last content page rather than continuous
scrolling. It is off by default and remembered on this device. The front page refreshes every
minute, or immediately when you tap **Refresh**.

Opening, navigating to or reloading a session transcript starts at the bottom;
e-ink starts on the last page. An explicit message anchor takes precedence.
After opening, the usual follow behaviour applies: new messages stay in view at
the end without pulling the reader down when they have scrolled up.

Session transcripts keep their title in a compact sticky breadcrumb bar while you
read. The title tooltip and the menu's Session section show the agent, all models
and session ID. Breadcrumb links return to the repository or parent session.
One context-aware button handles navigation:

- At the transcript's end, the button is hidden. In the normal display profile,
  arriving entries keep that end in view unless **Follow new messages** is off;
  scrolling up stops follow, and reaching
  the end by hand or by a jump restores it. This includes keyboard scrolling
  and multi-frame touch or trackpad gestures. Blank room after the transcript
  is not counted as unread content.
- **Latest answer ↓** jumps to the newest assistant reply's start when it is out
  of view and you are not already reading inside that reply. Its start aligns
  below the header, including any out-of-date banner. A long reply then offers
  **Scroll to bottom ↓**, which jumps to the end and resumes follow.
- **New messages ↓** takes priority when entries arrive while follow is off. It
  jumps to the first unseen entry's start if that entry is an answer; otherwise
  it jumps to the end. Follow resumes only if the jump reaches the transcript's
  end: jumping to a long answer's start leaves you there to read. E-ink keeps
  the new entries held until this tap.

A truncated title retains its accessible heading name.
Page turns reserve the header, bottom controls and device safe-area inset, even
on short landscape screens. A partial keyboard or system scroll stays on its
current page so Next never skips unread text. Answer jumps start a new page at
the reply, with subsequent page turns continuing from there; end jumps stop on
the last content page, not in the blank space reserved for short answer jumps.

The e-ink **Page controls** setting selects Indicator (the default), Side buttons,
or Bar. Edge tap zones and the n/m keys remain available in every mode, and the
bottom guard protects the device gesture area. **Page step** selects 100, 90,
75, 66 or 50 percent of the readable height; overlapping forward turns briefly
mark the previous page boundary with a hairline.

**Jump to…** in the menu or the `j` key opens a list of user prompts, with links
to Start, Latest answer and End. In Indicator mode, the page indicator opens it
too. Opening a transcript starts at its end (the last page on e-ink); an explicit
message anchor takes precedence. Reading positions are not remembered. **Show title bar** can hide the bar for more reading space; the
floating ⋮ remains available and offers a way back to the parent.

## Install as an app

The dashboard serves a web app manifest, so a browser can install it
("Install" or "Add to Home screen") and open it in its own window on a phone
or desktop. Installing needs the page to be served over HTTPS or from
`localhost`.

## Behind a reverse proxy

The server is meant to sit at the **root of a domain** (`https://jb.example.com/`),
not under a path prefix. Put TLS and the public name in the proxy and keep
`remote.web.listen` on `127.0.0.1`. The proxy must

- upgrade WebSocket connections for `/api/ws` (the live view depends on it),
- pass the original `Host` and `Origin` headers (FIDO2 derives its domain from
  them, and a WebSocket whose `Origin` does not match `Host` is refused),
- keep the connection open long enough for a live view.

```nginx
server {
    listen 443 ssl;
    server_name jb.example.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8910;
        proxy_set_header Host $host;
        proxy_set_header Origin $http_origin;
    }

    location /api/ws {
        proxy_pass http://127.0.0.1:8910;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header Origin $http_origin;
        proxy_read_timeout 1h;
    }
}
```

Every response carries a `Content-Security-Policy` (`default-src 'self'`,
`frame-ancestors 'none'`, no inline scripts or styles), `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. A proxy
that adds its own security headers should not loosen these; the dashboard
cannot be embedded in another page.

Keep authentication on behind a proxy. Every request the server sees comes
from the proxy, which is why [source addresses are never trusted](security.md#web-dashboard-authentication).
An SSH tunnel (`ssh -L 8910:127.0.0.1:8910 host`) works the same way.

The compact title bar provides breadcrumb links to parent views. In a transcript, the title tooltip and the Session section of Quick settings show the agent, models and session ID.
