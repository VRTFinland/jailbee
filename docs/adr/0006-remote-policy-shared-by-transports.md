# One remote policy model for every remote transport

The remote permission settings were shaped for SSH. They lived in `remote.ssh`, and child processes carried SSH-named session markers. They now form a transport-agnostic remote policy:

- commands;
- host restriction;
- repo visibility;
- mounts;
- orchestration;
- network unlock;
- files;
- RDP.

`remote.ssh` and `remote.web` each hold their own instance, checked by the same code. Child processes carry a transport-neutral remote-session marker plus the transport's name.

All authenticated SSH keys and all logged-in web devices have the same rights within their transport. `remote.web` defaults to the most restrictive policy and always restricts the host. See #110.

## Considered options

- **Make the web dashboard reuse `remote.ssh` directly.** Rejected. You may well want the web, which is reachable from a browser over the internet, to be stricter than SSH.
- **Give the web a separate, web-specific permission model.** Rejected. Two models drift, and a dashboard menu entry must be offered exactly when its command would pass, which is easiest with one check.

## Consequences

- The YAML keys of `remote.ssh` are unchanged, so no config migration is needed.
- SSH-only behaviour, such as waypipe, gpg and console hardening, keys off the transport name.
