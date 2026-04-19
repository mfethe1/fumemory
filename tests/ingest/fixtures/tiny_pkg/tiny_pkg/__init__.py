"""A tiny fixture package used to exercise codebase ingestion.

Its job is to provide a stable, minimal Python tree that covers enough
parser corners for memU tests: top-level classes and functions,
cross-module imports (absolute + relative), aliased module imports, a
class with multiple methods calling each other via ``self``, and a
private module that should be ingested but flagged as private.
"""
from .core import Greeter, make_greeter

__all__ = ["Greeter", "make_greeter"]
