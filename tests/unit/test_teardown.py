"""Unit tests for common/teardown.py.

Verifies is_not_found_error's suffix classification and run_step's
continue/halt behavior across a successful call, a not-found ClientError, a
non-not-found ClientError, and a waiter failure, per Requirements 3.4, 3.5,
7.6, and 7.7.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, create_autospec

import pytest
from botocore.exceptions import ClientError, WaiterError

from common.teardown import NOT_FOUND_CODES, NOT_FOUND_SUFFIXES, is_not_found_error, run_step


def make_client_error(code: str) -> ClientError:
    """Build a real ClientError with a controlled Error.Code, as botocore
    itself would construct one from an AWS API error response."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "boom"}},
        operation_name="DeleteThing",
    )


def make_waiter_error() -> WaiterError:
    """Build a real WaiterError, as botocore's waiter machinery would raise
    when a resource never reaches the awaited state within its configured
    attempts."""
    return WaiterError(name="thing_deleted", reason="Max attempts exceeded", last_response={})


class TestIsNotFoundError:
    def test_recognizes_not_found_suffix(self):
        err = make_client_error("InvalidVpcID.NotFound")
        assert is_not_found_error(err) is True

    def test_recognizes_not_found_fault_suffix(self):
        err = make_client_error("DBInstanceNotFoundFault")
        assert is_not_found_error(err) is True

    def test_recognizes_s3_no_such_key(self):
        # S3 doesn't follow the NotFound/NotFoundFault suffix convention --
        # a missing object returns NoSuchKey instead. Caught against a real
        # AWS account (s3_provisioner.py's final verification read), not
        # just hypothesized: the original suffix-only check misclassified
        # this as an "other" failure instead of the expected success case.
        err = make_client_error("NoSuchKey")
        assert is_not_found_error(err) is True

    def test_recognizes_s3_no_such_bucket(self):
        err = make_client_error("NoSuchBucket")
        assert is_not_found_error(err) is True

    def test_rejects_other_error_codes(self):
        err = make_client_error("DependencyViolation")
        assert is_not_found_error(err) is False

    def test_not_found_suffixes_constant(self):
        assert NOT_FOUND_SUFFIXES == ("NotFound", "NotFoundFault")

    def test_not_found_codes_constant(self):
        assert NOT_FOUND_CODES == ("NoSuchKey", "NoSuchBucket")


class TestRunStep:
    def test_successful_func_returns_true_and_does_not_log_error(self):
        logger = create_autospec(logging.Logger, instance=True)
        func = MagicMock(return_value=None)

        result = run_step(logger, "delete thing", func)

        assert result is True
        func.assert_called_once()
        logger.error.assert_not_called()

    def test_not_found_client_error_returns_true_and_logs_absence(self):
        logger = create_autospec(logging.Logger, instance=True)
        func = MagicMock(side_effect=make_client_error("InvalidSubnetID.NotFound"))

        result = run_step(logger, "delete subnet", func)

        assert result is True
        logger.error.assert_not_called()
        logger.info.assert_called_once()
        # The absence should be reflected in the logged message.
        logged_args = logger.info.call_args[0]
        assert "delete subnet" in logged_args

    def test_other_client_error_returns_false_and_logs_error(self):
        logger = create_autospec(logging.Logger, instance=True)
        func = MagicMock(side_effect=make_client_error("DependencyViolation"))

        result = run_step(logger, "delete route table", func)

        assert result is False
        logger.error.assert_called_once()
        logged_args = logger.error.call_args[0]
        assert "delete route table" in logged_args

    def test_waiter_error_returns_false_and_logs_error(self):
        logger = create_autospec(logging.Logger, instance=True)
        func = MagicMock(side_effect=make_waiter_error())

        result = run_step(logger, "wait for cluster deletion", func)

        assert result is False
        logger.error.assert_called_once()
        logged_args = logger.error.call_args[0]
        assert "wait for cluster deletion" in logged_args

    def test_run_step_never_raises_for_any_handled_exception(self):
        logger = create_autospec(logging.Logger, instance=True)

        for side_effect in (
            make_client_error("InvalidVpcID.NotFound"),
            make_client_error("DependencyViolation"),
            make_waiter_error(),
        ):
            func = MagicMock(side_effect=side_effect)
            # Should not raise; just assert it returns a bool either way.
            result = run_step(logger, "some step", func)
            assert isinstance(result, bool)

    def test_run_step_does_not_catch_unrelated_exceptions(self):
        logger = create_autospec(logging.Logger, instance=True)
        func = MagicMock(side_effect=ValueError("not a ClientError or WaiterError"))

        with pytest.raises(ValueError):
            run_step(logger, "some step", func)
