"""Bootstrap src-layout imports for pytest only if the package is not installed;
a correct `pip install -e .` makes this a no-op that never shadows the install."""
try:
    import converge_orchestrator  # noqa: F401
except ModuleNotFoundError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
