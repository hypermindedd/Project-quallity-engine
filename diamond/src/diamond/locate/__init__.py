"""Map a DOM element (or script URL) back to source code."""

from .source import SourceIndex, locate_finding, parse_location_string

__all__ = ["SourceIndex", "locate_finding", "parse_location_string"]
