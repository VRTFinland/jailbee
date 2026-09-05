"""The config editor: schema introspection, layer resolution, editor state.

Split so the parts that can be unit-tested are, following
`dashboard_settings.py`: everything here is pure data in, data out. The
terminal driver lives outside this package.
"""
