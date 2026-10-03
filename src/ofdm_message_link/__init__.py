"""One-way OFDM message link.

This package carries application bytes one way only: there is no ACK, no ARQ
and no TDD, so a lost or corrupted burst is simply lost.  ``README.md`` and
``docs/`` describe what the link does and what it deliberately leaves out.
"""
