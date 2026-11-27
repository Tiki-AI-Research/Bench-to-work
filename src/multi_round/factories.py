"""Load trusted Python factories explicitly declared in configuration."""

from importlib import import_module


def load_factory(reference: str):
    """Import an explicit module:function reference without directory scanning or plugin auto-discovery."""
    module, separator, name = reference.partition(":")
    if not separator or not module or not name:
        raise ValueError("Python factory reference must have the form 'module:function'")
    factory = getattr(import_module(module), name)
    if not callable(factory):
        raise ValueError(f"Python factory reference is not callable: {reference}")
    return factory
