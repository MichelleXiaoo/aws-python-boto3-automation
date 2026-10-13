"""
Shared region/secret/setting resolution for this project.

Every domain script needs exactly one documented region override
mechanism, honored identically, plus the same no-hardcoded-secret
discipline. This module is the single implementation each domain script's
``main()`` imports, so "identical behavior" is structural rather than 
six independent copies that could drift.
"""

from __future__ import annotations

import os

import boto3

from common.exceptions import InvalidRegionError, MissingSecretError

#: The default region every domain script falls back to when no override is provided.
DEFAULT_REGION = "ap-southeast-2"

#: The one documented region override mechanism: the name of the
#: environment variable every domain script reads via
#: :func:`resolve_region`. Deliberately project-specific (not boto3's own
#: ``AWS_DEFAULT_REGION``) so it cannot collide with boto3's own,
#: separate, unrelated region resolution for credentials.
REGION_ENV_VAR = "PORTFOLIO_AWS_REGION"


def resolve_region() -> str:
    """
    Return the effective region for the calling script's AWS clients.

    Reads :data:`REGION_ENV_VAR` from the environment. If it is unset,
    returns :data:`DEFAULT_REGION`. If it is set, validates it against
    ``boto3.Session().get_available_regions("ec2")`` -- a local botocore
    data lookup that performs no network call -- and returns it if valid.

    This function never constructs a ``boto3.client(...)``; it only builds
    a ``boto3.Session()`` to query the locally bundled region data. That
    guarantees an invalid override is rejected before any region-scoped
    client exists for that invocation.

    Returns:
        The resolved region string: either the validated override or
        :data:`DEFAULT_REGION`.

    Raises:
        InvalidRegionError: If :data:`REGION_ENV_VAR` is set to a value
        that is not a valid AWS region identifier.
    """
    override = os.environ.get(REGION_ENV_VAR)
    if not override:
        return DEFAULT_REGION

    available_regions = boto3.Session().get_available_regions("ec2")
    if override not in available_regions:
        raise InvalidRegionError(
            f"{REGION_ENV_VAR}={override!r} is not a valid AWS region identifier."
        )

    return override


def require_env(var_name: str) -> str:
    """
    Return a required secret environment variable's value.

    Treats an unset variable and an empty-string value identically: both
    count as "missing," so a script cannot silently proceed with an empty secret.

    Args:
        var_name: The name of the required environment variable.

    Returns:
        The variable's value.

    Raises:
        MissingSecretError: If the variable is unset or set to an empty
        string. The raised error names ``var_name`` without exposing
        any default or fallback value.
    """
    value = os.environ.get(var_name)
    if not value:
        raise MissingSecretError(
            f"Required environment variable '{var_name}' is not set."
        )
    return value


def get_setting(var_name: str, default: str) -> str:
    """
    Return a configurable, non-secret setting's value.

    Used for per-domain constants that have a sensible documented default
    but should be overridable without editing source, such as EC2's AMI id
    and key pair name.

    Args:
        var_name: The name of the environment variable that overrides ``default``, if set.
        default: The value to return when ``var_name`` is unset.

    Returns:
        ``os.environ.get(var_name, default)``.
    """
    return os.environ.get(var_name, default)
