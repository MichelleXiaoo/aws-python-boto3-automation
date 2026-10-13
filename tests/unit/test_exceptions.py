"""Unit tests for common/exceptions.py.

Verifies each exception class instantiates correctly, derives from
PortfolioError, and exposes the attributes described in the design's
Components and Interfaces / Error Handling sections.
"""

import pytest

from common.exceptions import (
    AmbiguousResourceError,
    InvalidRegionError,
    MissingSecretError,
    PortfolioError,
    ResourceCreationError,
)


def test_portfolio_error_is_an_exception():
    assert issubclass(PortfolioError, Exception)
    err = PortfolioError("something went wrong")
    assert str(err) == "something went wrong"


@pytest.mark.parametrize("exc_class", [InvalidRegionError, MissingSecretError])
def test_simple_subclasses_derive_from_portfolio_error(exc_class):
    assert issubclass(exc_class, PortfolioError)
    err = exc_class("detail message")
    assert isinstance(err, PortfolioError)
    assert str(err) == "detail message"


def test_invalid_region_error_can_be_raised_and_caught_as_portfolio_error():
    with pytest.raises(PortfolioError):
        raise InvalidRegionError("not-a-real-region")


def test_missing_secret_error_can_be_raised_and_caught_as_portfolio_error():
    with pytest.raises(PortfolioError):
        raise MissingSecretError("RDS_MASTER_PASSWORD")


def test_ambiguous_resource_error_derives_from_portfolio_error():
    assert issubclass(AmbiguousResourceError, PortfolioError)


def test_ambiguous_resource_error_exposes_attributes():
    err = AmbiguousResourceError("vpc", ["vpc-111", "vpc-222"])
    assert err.resource_type == "vpc"
    assert err.matching_ids == ["vpc-111", "vpc-222"]


def test_ambiguous_resource_error_message_names_type_and_ids():
    err = AmbiguousResourceError("subnet", ["subnet-aaa", "subnet-bbb"])
    message = str(err)
    assert "subnet" in message
    assert "subnet-aaa" in message
    assert "subnet-bbb" in message


def test_ambiguous_resource_error_can_be_caught_as_portfolio_error():
    with pytest.raises(PortfolioError):
        raise AmbiguousResourceError("route_table", ["rtb-1", "rtb-2"])


def test_resource_creation_error_derives_from_portfolio_error():
    assert issubclass(ResourceCreationError, PortfolioError)


def test_resource_creation_error_exposes_attributes():
    cause = ValueError("boom")
    err = ResourceCreationError("internet_gateway", cause)
    assert err.resource_type == "internet_gateway"
    assert err.cause is cause


def test_resource_creation_error_message_names_resource_type_and_cause():
    cause = ValueError("boom")
    err = ResourceCreationError("vpc", cause)
    message = str(err)
    assert "vpc" in message
    assert "boom" in message


def test_resource_creation_error_chains_cause():
    cause = ValueError("boom")
    err = ResourceCreationError("vpc", cause)
    # __cause__ is set so standard exception chaining/traceback tooling
    # reports the original error even without an explicit `raise ... from`.
    assert err.__cause__ is cause


def test_resource_creation_error_can_be_caught_as_portfolio_error():
    with pytest.raises(PortfolioError):
        raise ResourceCreationError("subnet", RuntimeError("create failed"))
