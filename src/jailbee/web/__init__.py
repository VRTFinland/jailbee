"""The web dashboard server (#109): host-side only, behind the optional `web` extra.

Import-free on purpose: `jailbee.cli` imports `jailbee.web.assets` and
`jailbee.web.errors` (standard library only) before it knows whether FastAPI
is installed. Neither the container CLI nor `jailbee --help` imports anything
under this package.
"""
