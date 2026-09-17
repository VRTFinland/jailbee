"""Agent account pools: a generic engine plus one adapter per agent.

Nothing is re-exported here. `cli.py` imports the submodules lazily inside
command functions so `jailbee --help` stays fast, and a package-level import
of `engine` would pull in the database and Incus wrapper on every run.
"""
