"""Feature scripts package.

All standalone automation entry points (CLI helpers, batch scripts, etc.) live in
this package so they can share common imports and be executed via
``python -m feature.<script_name>``.  Drop new scripts in this directory to make
them importable as ``feature.<module>`` across the Django project.
"""

__all__: list[str] = []
