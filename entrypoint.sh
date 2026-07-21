#!/bin/sh
# Ensure /data (and any pre-existing DB/journal files) are writable by appuser
# regardless of host mount ownership. Recursive so a root-owned sync.db left by
# an earlier run can't make SQLite report "attempt to write a readonly database".
chown -R appuser /data 2>/dev/null || true
exec gosu appuser "$@"
