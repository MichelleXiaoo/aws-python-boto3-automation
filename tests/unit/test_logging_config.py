"""Unit tests for common.logging_config.get_logger."""

from __future__ import annotations

import logging

from common.logging_config import get_logger


def test_get_logger_enables_info_level():
    """The returned logger's level is INFO, so both INFO and ERROR messages
    are emitted."""
    logger = get_logger("test_logger_info_level")

    assert logger.level == logging.INFO
    assert logger.isEnabledFor(logging.INFO)
    assert logger.isEnabledFor(logging.ERROR)


def test_get_logger_is_idempotent_against_duplicate_handlers():
    """Calling get_logger twice with the same name must not duplicate
    handlers."""
    first = get_logger("test_logger_idempotent")
    second = get_logger("test_logger_idempotent")

    assert first is second
    assert len(first.handlers) == 1


def test_get_logger_handler_uses_expected_format():
    """The attached StreamHandler must be formatted exactly as
    '%(asctime)s %(levelname)s %(name)s: %(message)s'."""
    logger = get_logger("test_logger_format")

    assert len(logger.handlers) == 1
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)

    formatter = handler.formatter
    assert formatter is not None
    assert formatter._fmt == "%(asctime)s %(levelname)s %(name)s: %(message)s"

    # Confirm the format actually produces the expected shape when applied
    # to a real log record.
    record = logging.LogRecord(
        name="test_logger_format",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello world",
        args=None,
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert " INFO test_logger_format: hello world" in formatted
