"""Hermes plugin entry point for the shared Team0 agent runtime."""

# Hermes imports directory plugins as packages. Pytest also imports this file
# while collecting sibling tests, but without a package context.
if __package__:
    from .hermes_adapter import register
