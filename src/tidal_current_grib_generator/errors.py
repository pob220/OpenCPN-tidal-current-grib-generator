"""Project-specific exceptions."""


class TidalCurrentGribError(Exception):
    """Base error for user-facing failures."""


class ValidationError(TidalCurrentGribError):
    """Input validation failed."""


class MissingDependencyError(TidalCurrentGribError):
    """An optional runtime dependency is required."""


class UnsupportedSourceError(TidalCurrentGribError):
    """A requested current source is not available."""
