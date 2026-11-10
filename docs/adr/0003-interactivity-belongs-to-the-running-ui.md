# Interactivity belongs to the UI that runs the command

A dashboard runs an action by starting JailBee's own CLI as a child process. Until now that child asked its questions on whatever terminal it held. Every JailBee question now goes through the `prompting` module. When a dashboard starts the command, the questions travel over a **prompt channel** to that dashboard: an inherited socketpair carrying typed JSON-line messages. The terminal dashboard shows them as modals, and the web dashboard relays them to the browser. One command implementation thereby serves every UI, and the CLI on a terminal behaves as before. See #110.

## Considered options

- **Hooks or per-UI dialogs that pre-ask the questions and pass flags.** This is what the Qt dashboard does. Rejected: every UI duplicates the CLI's question logic, and the copies drift from repo config defaults.
- **Hand the child a terminal**, a browser terminal for the web. Rejected as the default: native dialogs are unusable that way on e-ink and phones. It stays the route for attach-style actions only (shell, attach, GUI).
- **A named socket in the runtime directory** instead of an inherited socketpair. Rejected: it leaves files to clean up, and other processes could reach it. The asker is always the dashboard's own child, so a name buys nothing.

## Consequences

- A test forbids prompt libraries outside `prompting`. The exceptions are named.
- The terminal dashboard stops releasing the terminal for non-attach actions. Their output is shown by the dashboard instead.
