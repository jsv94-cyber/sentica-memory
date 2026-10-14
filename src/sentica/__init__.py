"""Sentica single-tenant instance controls.

``SENTICA_MODE`` must be ``production`` or ``test``. Any other value,
including unset, refuses to start. Production is the single-tenant instance.
Test keeps the upstream behavior for the suite.
"""
