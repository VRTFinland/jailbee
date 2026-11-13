# Coding standards

Judgement rules for review. Mechanical rules live in ruff, mypy and the test suite; architecture rules are in `CLAUDE.md`.

## The host–container trust boundary

A container is untrusted. The host acts on its requests with the host user's credentials (the `gh` token, Incus, the broker).

- **Scope a host-credentialed read before it runs.** When a container request makes the host read something with host credentials (GitHub, Incus state, another container), check that the request may see that target first, for example against the repo's own config, a host-local list or the requester's grant. Only then read. Data read with the host token that ends up in a manifest, a status reply or `show` output goes back to the container.
- **Refuse before reading, not after.** A refusal that comes after the read has still spent the token, and its error text can leak what the read found.

## Locks and transactions

- **No network or subprocess call inside a database lock or transaction.** Do `gh`, GraphQL, `incus` or `git` calls first, outside the lock. Then take the lock, revalidate and write. A slow call inside `BEGIN IMMEDIATE` stalls every other writer of that store.

## Retries after partial success

- **A retry reuses the receipts of the steps that already succeeded.** When one staged action makes several remote writes (add an item, then set its fields), record each write's receipt as it lands. A retry continues from those receipts instead of assuming none or all of the writes happened.
