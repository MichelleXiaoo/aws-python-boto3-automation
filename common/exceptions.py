"""
Shared exception hierarchy for this project.

Every custom exception raised by this project's own scripts derives from
:class:`PortfolioError`, so a caller can write ``except PortfolioError`` to
catch "a problem this project detected" distinctly from
``botocore.exceptions.ClientError`` ("AWS rejected a call").
"""

from __future__ import annotations


class PortfolioError(Exception):
    """
    Base class for every exception raised by this project's own code.

    Catching ``PortfolioError`` distinguishes problems this project detected
    (invalid configuration, ambiguous resource state, a failed creation call)
    from ``botocore.exceptions.ClientError``, which signals that AWS itself
    rejected a call.
    """


class InvalidRegionError(PortfolioError):
    """
    Raised by ``common.config.resolve_region`` when the region override
    environment variable is set but is not a valid AWS region identifier.

    Always raised before any ``boto3.client(...)`` call is constructed in
    that invocation.
    """


class MissingSecretError(PortfolioError):
    """
    Raised by ``common.config.require_env`` when a required secret
    environment variable is unset or empty.

    Always raised before any ``boto3.client(...)`` call is constructed in
    that invocation.
    """


class AmbiguousResourceError(PortfolioError):
    """
    Raised when a describe-then-create check finds 2+ resources matching
    a resource's identifying tag filter.

    Attributes:
        resource_type: The kind of resource being checked (e.g. ``"vpc"``, ``"subnet"``).
        matching_ids: The identifiers of every resource that matched the describe filter.
    """

    def __init__(self, resource_type: str, matching_ids: list[str]) -> None:
        self.resource_type = resource_type
        self.matching_ids = matching_ids
        message = (
            f"Ambiguous match for resource type '{resource_type}': "
            f"found {len(matching_ids)} matching resources {matching_ids!r}, "
            "expected at most one."
        )
        super().__init__(message)


class ResourceCreationError(PortfolioError):
    """
    Raised when a ``create_*`` call fails with an underlying error
    (typically ``botocore.exceptions.ClientError``).

    Wraps the original error as ``cause`` and names the resource type that
    failed to create, so the ordered creation sequence halts with a message
    that identifies which step failed.

    Attributes:
        resource_type: The kind of resource that failed to create (e.g. ``"vpc"``, "internet gateway").
        cause: The underlying exception that caused the creation to fail.
    """

    def __init__(self, resource_type: str, cause: Exception) -> None:
        self.resource_type = resource_type
        self.cause = cause
        message = f"Failed to create resource type '{resource_type}': {cause}"
        super().__init__(message)
        # Preserve the standard exception-chaining link (same effect as
        # `raise ResourceCreationError(...) from cause`) even for callers
        # that construct the exception without using the `from` clause.
        self.__cause__ = cause
