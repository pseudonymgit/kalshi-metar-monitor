"""Load an archived implementation without copying it into the active tree.

This compatibility loader is intentionally narrow: it preserves import paths for
tests and legacy callers while the cleanup decision remains pending.  It does
not mutate the archived source or execute any external side effect by itself.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_archived(relative_path: str, loader_name: str):
    source = Path(__file__).parent / "archive" / relative_path
    if relative_path.startswith("../"):
        source = Path(__file__).parent / relative_path[3:]
    spec = spec_from_file_location(loader_name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"archived compatibility source unavailable: {source}")
    module = module_from_spec(spec)
    # Give archived signal modules the active package context so their relative
    # imports resolve to the canonical BaseSignal/helpers.
    if loader_name.startswith("core.signals."):
        module.__package__ = "core.signals"
    spec.loader.exec_module(module)
    return module


def export_archived(public_globals: dict, relative_path: str, loader_name: str) -> None:
    module = load_archived(relative_path, loader_name)
    public_globals.update(
        {key: value for key, value in vars(module).items() if not key.startswith("__")}
    )
