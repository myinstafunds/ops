"""Upstream integrations.

Only BRE is wired in Release 0/1. The LOS and LMS packages arrive with
Releases 3 and 4 and must follow the same rules: read only, independently
failing, and never writing customer journey state.
"""

__all__: list[str] = []
