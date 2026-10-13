"""Unit tests for common/config.py.

Verifies resolve_region's override/default/invalid-override behavior
(without any boto3.client(...) call ever being constructed on the invalid
path), require_env's set/unset/empty-string handling, and get_setting's
override/default behavior, per Requirements 5.8, 6.3, 10.4, 11.1-11.4.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from common.config import DEFAULT_REGION, REGION_ENV_VAR, get_setting, require_env, resolve_region
from common.exceptions import InvalidRegionError, MissingSecretError


class TestResolveRegion:
    def test_returns_default_when_override_unset(self, monkeypatch):
        monkeypatch.delenv(REGION_ENV_VAR, raising=False)

        assert resolve_region() == DEFAULT_REGION

    def test_returns_override_when_valid(self, monkeypatch):
        monkeypatch.setenv(REGION_ENV_VAR, "us-west-2")

        # Patch at the point of use so the test is independent of botocore's
        # actual bundled region data (and still fast/offline).
        with patch("common.config.boto3.Session") as mock_session_cls:
            mock_session_cls.return_value.get_available_regions.return_value = [
                "us-west-2",
                "ap-southeast-2",
            ]
            assert resolve_region() == "us-west-2"

    def test_raises_invalid_region_error_when_override_invalid(self, monkeypatch):
        monkeypatch.setenv(REGION_ENV_VAR, "not-a-real-region")

        with patch("common.config.boto3.Session") as mock_session_cls:
            mock_session_cls.return_value.get_available_regions.return_value = [
                "us-west-2",
                "ap-southeast-2",
            ]
            with pytest.raises(InvalidRegionError):
                resolve_region()

    def test_invalid_override_never_constructs_a_boto3_client(self, monkeypatch):
        """resolve_region() must reject an invalid override using only the
        local Session().get_available_regions() lookup, never constructing a
        boto3.client(...)."""
        monkeypatch.setenv(REGION_ENV_VAR, "not-a-real-region")

        with patch("common.config.boto3.Session") as mock_session_cls, patch(
            "common.config.boto3.client"
        ) as mock_client:
            mock_session_cls.return_value.get_available_regions.return_value = [
                "ap-southeast-2"
            ]
            with pytest.raises(InvalidRegionError):
                resolve_region()

            mock_client.assert_not_called()

    def test_empty_string_override_falls_back_to_default(self, monkeypatch):
        """An empty-string override is treated the same as unset."""
        monkeypatch.setenv(REGION_ENV_VAR, "")

        assert resolve_region() == DEFAULT_REGION

    def test_invalid_region_error_message_names_the_offending_value(self, monkeypatch):
        monkeypatch.setenv(REGION_ENV_VAR, "mars-central-1")

        with patch("common.config.boto3.Session") as mock_session_cls:
            mock_session_cls.return_value.get_available_regions.return_value = [
                "ap-southeast-2"
            ]
            with pytest.raises(InvalidRegionError, match="mars-central-1"):
                resolve_region()


class TestRequireEnv:
    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("SOME_SECRET", "s3cr3t-value")

        assert require_env("SOME_SECRET") == "s3cr3t-value"

    def test_raises_missing_secret_error_when_unset(self, monkeypatch):
        monkeypatch.delenv("SOME_SECRET", raising=False)

        with pytest.raises(MissingSecretError, match="SOME_SECRET"):
            require_env("SOME_SECRET")

    def test_raises_missing_secret_error_when_empty_string(self, monkeypatch):
        monkeypatch.setenv("SOME_SECRET", "")

        with pytest.raises(MissingSecretError, match="SOME_SECRET"):
            require_env("SOME_SECRET")

    def test_missing_secret_error_does_not_expose_a_default_value(self, monkeypatch):
        monkeypatch.delenv("RDS_MASTER_PASSWORD", raising=False)

        with pytest.raises(MissingSecretError) as exc_info:
            require_env("RDS_MASTER_PASSWORD")

        message = str(exc_info.value)
        assert "RDS_MASTER_PASSWORD" in message


class TestGetSetting:
    def test_returns_override_when_set(self, monkeypatch):
        monkeypatch.setenv("EC2_AMI_ID", "ami-0123456789abcdef0")

        assert get_setting("EC2_AMI_ID", "ami-default") == "ami-0123456789abcdef0"

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("EC2_KEY_PAIR_NAME", raising=False)

        assert get_setting("EC2_KEY_PAIR_NAME", "default-key-pair") == "default-key-pair"
