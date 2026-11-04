"""Migration-managed app; the initial schema still requires explicit approval.

An empty package prevents Django from treating this app as unmigrated and
creating its tables through syncdb. No schema migration is defined here.
"""
