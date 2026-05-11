"""Pure-Python helpers used by the rest of the `feature` app.

Modules here must not import from Django apps — that keeps them safe to
call from migrations, signal handlers, and tests without dragging in the
ORM. They are the canonical home of any small formula or transformation
that `feature` controllers (and `data` write paths via signals) need to
share.
"""
