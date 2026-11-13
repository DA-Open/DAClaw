"""Tool connectors: the adapters that talk to the outside world (spec 5.2).

A connector only executes an ``AuthorizedCall`` the gateway has checked and
registered; it never decides permissions, budget or task status.
"""

from .files import ReadFileConnector, WriteFileConnector

__all__ = ["ReadFileConnector", "WriteFileConnector"]
