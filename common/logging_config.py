"""
Shared logging configuration for this project.

Every non-legacy script calls :func:`get_logger` instead of configuring its
own ``logging`` setup, so that an INFO/ERROR discipline (status and success
messages at INFO, caught exceptions at ERROR) is implemented once and
applied identically across all domain scripts.
"""

from __future__ import annotations

import logging

#: Format shared by every logger this module hands out: timestamp, level,
#: logger name, and message.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def get_logger(name: str) -> logging.Logger:
    """
    Return a module-named logger configured for console output.

    The returned logger has its level set to ``INFO`` (so both
    ``logger.info(...)`` and ``logger.error(...)`` calls are emitted) and has
    exactly one :class:`logging.StreamHandler` attached, formatted as
    ``'%(asctime)s %(levelname)s %(name)s: %(message)s'``.

    This function is idempotent: calling it more than once for the same
    ``name`` returns the same underlying logger and never attaches a second
    handler, so repeated calls (e.g. from multiple modules importing the same
    package, or re-running ``main()`` in a long-lived process) do not cause
    duplicate log lines.

    Args:
        name: The logger name, conventionally the calling module's ``__name__``.

    Returns:
        A configured ``logging.Logger`` instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Guard against duplicate handler registration: if a handler is already
    # attached (from a prior call with the same name), skip adding another.
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)

    return logger
