# Root conftest.py so pytest's rootdir-based sys.path insertion includes the
# project root (which contains the `common` package), regardless of whether
# pytest is invoked as `pytest`, `pytest tests/unit/`, or `python -m pytest`.
