"""
Shared teardown-step orchestration for this project.

The VPC and RDS teardown scripts both need the exact same not-found/
other-error classification rule: if the resource a step targets is
already gone, log that absence and continue with the next
step; for any other failure, log the error and halt the remaining steps.

Implementing this once here, instead of once per teardown script, is what
makes "same not-found/halt rule" structural rather than duplicated (see the
design's "Teardown step runner" flow diagram).
"""

from __future__ import annotations

import logging
from typing import Callable

from botocore.exceptions import ClientError, WaiterError

#: AWS error codes that indicate "the targeted resource does not exist"
#: rather than some other failure. Botocore error codes for not-found
#: conditions commonly end in one of these two suffixes, e.g.
#: ``InvalidVpcID.NotFound`` or ``DBInstanceNotFoundFault``.
NOT_FOUND_SUFFIXES = ("NotFound", "NotFoundFault")

#: AWS error codes that indicate the same "resource does not exist"
#: condition but don't follow the suffix convention above -- S3 uses its
#: own, unsuffixed codes for this (``NoSuchKey`` for a missing object,
#: ``NoSuchBucket`` for a missing bucket), matched exactly rather than by
#: suffix.
NOT_FOUND_CODES = ("NoSuchKey", "NoSuchBucket")


def is_not_found_error(err: ClientError) -> bool:
    """
    Return True if ``err`` represents a resource-not-found condition.

    Inspects ``err.response["Error"]["Code"]`` and returns True if it ends
    with any of :data:`NOT_FOUND_SUFFIXES`, or exactly matches any of
    :data:`NOT_FOUND_CODES`.

    Args:
        err: The ``ClientError`` raised by a boto3 call.

    Returns:
        True if the error code is a recognized not-found condition, else
        False.
    """
    code = err.response.get("Error", {}).get("Code", "")
    return code.endswith(NOT_FOUND_SUFFIXES) or code in NOT_FOUND_CODES


def run_step(logger: logging.Logger, step_name: str, func: Callable[[], None]) -> bool:
    """
    Run one teardown step and classify the outcome as continue or halt.

    Calls ``func()``. On success, or on a ``ClientError`` whose error code
    is a not-found condition (the resource is already absent), this returns
    True so the caller's teardown loop continues with the next step. On any
    other ``ClientError``, or on a ``WaiterError`` (a boto3 waiter that
    failed to confirm completion within its configured attempts), this logs
    the error and returns False so the caller's teardown loop halts without
    running any remaining steps.

    This function never re-raises: every exception it catches is logged and
    converted into a boolean continue/halt signal, matching "log and
    continue" / "halt without raising an unhandled exception" for the VPC
    and RDS teardown scripts alike.

    Args:
        logger: The logger to record the step's outcome on.
        step_name: A human-readable name for the step, used in log messages.
        func: A zero-argument callable that performs the step's AWS call(s).
            Its return value is ignored.

    Returns:
        True if the teardown sequence should continue with the next step,
        False if it should halt.
    """
    try:
        func()
    except ClientError as err:
        if is_not_found_error(err):
            logger.info("%s: resource already absent, continuing.", step_name)
            return True
        logger.error("%s: failed with error: %s", step_name, err)
        return False
    except WaiterError as err:
        logger.error("%s: waiter failed to confirm completion: %s", step_name, err)
        return False
    else:
        return True
