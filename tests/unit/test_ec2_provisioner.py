"""Unit tests for ec2/ec2_provisioner.py.

Covers find_existing_instance's state+tag matching (including multiple
reservations/instances and the no-match case), ensure_instance's
reuse-vs-create decision and its ClientError-caught-without-raising
behavior, and main()'s overall acquire-and-leave-running sequencing --
these are the SMOKE/EXAMPLE scenarios; the full Hypothesis property-test
suite is intentionally out of scope here.

The boto3 EC2 client is mocked throughout via unittest.mock; no real AWS
calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from ec2.ec2_provisioner import (
    DEFAULT_AMI_ID,
    DEFAULT_KEY_PAIR_NAME,
    INSTANCE_NAME,
    INSTANCE_TYPE,
    ensure_instance,
    find_existing_instance,
    main,
)


def make_client_error(operation_name: str = "SomeOperation") -> ClientError:
    """Build a real ClientError, as botocore itself would construct one
    from an AWS API error response."""
    return ClientError(
        error_response={"Error": {"Code": "InternalError", "Message": "boom"}},
        operation_name=operation_name,
    )


def make_instance(instance_id: str, name: str | None = None) -> dict:
    """Build a minimal describe_instances-shaped instance dict."""
    tags = [{"Key": "Name", "Value": name}] if name is not None else []
    return {"InstanceId": instance_id, "Tags": tags}


class TestFindExistingInstance:
    def test_matches_by_state_and_tag(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [make_instance("i-match", INSTANCE_NAME)]}
            ]
        }

        instance_id = find_existing_instance(ec2, INSTANCE_NAME)

        assert instance_id == "i-match"
        call_kwargs = ec2.describe_instances.call_args.kwargs
        assert call_kwargs["Filters"] == [
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            }
        ]

    def test_returns_none_when_no_match(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [make_instance("i-other", "some-other-name")]}
            ]
        }

        instance_id = find_existing_instance(ec2, INSTANCE_NAME)

        assert instance_id is None

    def test_returns_none_when_no_reservations(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": []}

        instance_id = find_existing_instance(ec2, INSTANCE_NAME)

        assert instance_id is None

    def test_matches_across_multiple_reservations_and_instances(self):
        """A match later in the response (second reservation, second
        instance) is still found."""
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        make_instance("i-unrelated-1", "other-name-1"),
                        make_instance("i-unrelated-2", "other-name-2"),
                    ]
                },
                {
                    "Instances": [
                        make_instance("i-unrelated-3", None),
                        make_instance("i-match", INSTANCE_NAME),
                    ]
                },
            ]
        }

        instance_id = find_existing_instance(ec2, INSTANCE_NAME)

        assert instance_id == "i-match"

    def test_returns_first_match_when_multiple_match(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        make_instance("i-first", INSTANCE_NAME),
                        make_instance("i-second", INSTANCE_NAME),
                    ]
                }
            ]
        }

        instance_id = find_existing_instance(ec2, INSTANCE_NAME)

        assert instance_id == "i-first"


class TestEnsureInstance:
    def test_reuses_when_existing_instance_found(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [make_instance("i-existing", INSTANCE_NAME)]}
            ]
        }

        instance_id = ensure_instance(ec2, INSTANCE_NAME, DEFAULT_AMI_ID, DEFAULT_KEY_PAIR_NAME)

        assert instance_id == "i-existing"
        ec2.run_instances.assert_not_called()

    def test_creates_when_no_existing_instance(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": []}
        ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-new"}]}

        instance_id = ensure_instance(ec2, INSTANCE_NAME, DEFAULT_AMI_ID, DEFAULT_KEY_PAIR_NAME)

        assert instance_id == "i-new"
        ec2.run_instances.assert_called_once()
        call_kwargs = ec2.run_instances.call_args.kwargs
        assert call_kwargs["ImageId"] == DEFAULT_AMI_ID
        assert call_kwargs["MinCount"] == 1
        assert call_kwargs["MaxCount"] == 1
        assert call_kwargs["InstanceType"] == INSTANCE_TYPE
        assert call_kwargs["KeyName"] == DEFAULT_KEY_PAIR_NAME
        assert call_kwargs["TagSpecifications"] == [
            {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": INSTANCE_NAME}]}
        ]

    def test_client_error_on_create_is_caught_and_signals_failure(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": []}
        ec2.run_instances.side_effect = make_client_error("RunInstances")

        instance_id = ensure_instance(ec2, INSTANCE_NAME, DEFAULT_AMI_ID, DEFAULT_KEY_PAIR_NAME)

        assert instance_id is None


class TestMain:
    def test_main_resolves_region_before_constructing_client(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [make_instance("i-existing", INSTANCE_NAME)]}
            ]
        }

        call_order = []
        with patch(
            "ec2.ec2_provisioner.resolve_region",
            side_effect=lambda: call_order.append("resolve_region") or "ap-southeast-2",
        ), patch(
            "ec2.ec2_provisioner.boto3.client",
            side_effect=lambda *a, **kw: call_order.append("boto3.client") or ec2,
        ):
            main()

        assert call_order == ["resolve_region", "boto3.client"]

    def test_main_reuses_existing_instance_and_does_not_terminate(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [make_instance("i-existing", INSTANCE_NAME)]}
            ]
        }

        with patch("ec2.ec2_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "ec2.ec2_provisioner.boto3.client", return_value=ec2
        ) as mock_client:
            result = main()

        mock_client.assert_called_once_with("ec2", region_name="ap-southeast-2")
        ec2.run_instances.assert_not_called()
        ec2.terminate_instances.assert_not_called()
        assert result == "i-existing"

    def test_main_creates_new_instance_and_does_not_terminate(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": []}
        ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-new"}]}

        with patch("ec2.ec2_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "ec2.ec2_provisioner.boto3.client", return_value=ec2
        ):
            result = main()

        ec2.run_instances.assert_called_once()
        ec2.terminate_instances.assert_not_called()
        assert result == "i-new"

    def test_main_returns_none_when_acquisition_fails(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": []}
        ec2.run_instances.side_effect = make_client_error("RunInstances")

        with patch("ec2.ec2_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "ec2.ec2_provisioner.boto3.client", return_value=ec2
        ):
            result = main()  # must not raise

        assert result is None

    def test_main_uses_configured_ami_and_key_name_settings(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": []}
        ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-new"}]}

        with patch("ec2.ec2_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "ec2.ec2_provisioner.boto3.client", return_value=ec2
        ), patch(
            "ec2.ec2_provisioner.get_setting",
            side_effect=lambda var_name, default: {
                "EC2_AMI_ID": "ami-custom",
                "EC2_KEY_PAIR_NAME": "custom-key",
            }[var_name],
        ):
            main()

        call_kwargs = ec2.run_instances.call_args.kwargs
        assert call_kwargs["ImageId"] == "ami-custom"
        assert call_kwargs["KeyName"] == "custom-key"
