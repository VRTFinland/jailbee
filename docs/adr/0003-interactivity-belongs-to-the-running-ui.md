# Interactivity belongs to the UI that runs the command

A dashboard runs an action by starting JailBee's own CLI as a child process. Until now that child asked its questions on whatever terminal it held. Every JailBee question now goes through the `prompting` module. The state service starts dashboard actions; their questions travel over a **prompt channel** to the service: an inherited socketpair carrying typed JSON-line messages. The terminal dashboard shows them as modals, and the web dashboard relays them to the browser. One command implementation thereby serves every UI, and the CLI on a terminal behaves as before. See #110.

## Considered options

- **Hooks or per-UI dialogs that pre-ask the questions and pass flags.** This is what the Qt dashboard does. Rejected: every UI duplicates the CLI's question logic, and the copies drift from repo config defaults.
- **Hand the child a terminal**, a browser terminal for the web. Rejected as the default: native dialogs are unusable that way on e-ink and phones. It stays the route for attach-style actions only (shell, attach, GUI).
- **A named socket in the runtime directory** instead of an inherited socketpair. Rejected: it leaves files to clean up, and other processes could reach it. The asker is always the action host's own child, so a name buys nothing.

## Consequences

- Background dashboard actions are now started by the state service through
  `dashboard.runner`; the service holds the child's end of the UI boundary,
  while dashboards receive entry changes and output through `StateClient`.
  Attach-style actions still use the requesting terminal. Answer arbitration
  and interaction tracking are frontend-agnostic dashboard core components.
  The service publishes pending questions to dashboards and arbitrates the first
  answer. Pending questions are never persisted with action entries.
- A test forbids prompt libraries outside `prompting`. The exceptions are named.
- The terminal dashboard stops releasing the terminal for non-attach actions. Their output is shown by the dashboard instead.
- Owning a question includes deciding when to show it. A question from a background action waits in the notification queue as a pending question, while the command's prompt handler stays blocked; answering the entry resolves only that command's question. It opens at once only when the user is with that action. The show-now decision is a pure function in the frontend-agnostic dashboard core, so every dashboard applies it the same way. The session owns question arrival and queue mutation; entry ownership travels with the question, not through frontend state. The frontend reports user interaction, overlay visibility and suspension explicitly. Dispatch or an answer grants immediate follow-up questions only until the next interaction; suspension always defers them. Later leaves the answer future unresolved, explicit abort resolves it with cancellation, and channel EOF removes a dead command's pending question. The originating dashboard applies this routing decision; other dashboards keep the question pending. Any dashboard can answer or abort, and the first answer wins. Later is local to the dashboard that chose it.
