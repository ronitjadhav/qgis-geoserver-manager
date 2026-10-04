#! python3  # noqa: E265

"""Plugin utilities.

Deliberately empty: importing a submodule must not pull in QGIS. That keeps
the pure ones (env_var_parser) importable, and unit-testable, on a plain
Python, which the CI unit job is.
"""
