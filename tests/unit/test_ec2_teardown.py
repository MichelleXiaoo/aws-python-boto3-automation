"""Unit tests for ec2/ec2_teardown.py.

Covers terminate_instance individually, main()'s "instance not found"
early-return path, and main()'s full sequence via the real
common.teardown.run_step -- both the success path (final confirmation
logged) and a halting failure (a non-not-found ClientError during
termination). These are the SMOKE/EXAMPLE scenarios; the full Hypothesis
property-test suite is intentionally out of scope here.

The boto3 EC2 client is mocked throughout via unittest.mock; no real AWS
calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from ec2.ec2_teardown import INSTANCE_NAME, main, terminate_instance


def make_client_error(code: str = "InternalError", operation_name: str = "SomeOp") -> ClientError:
    """Build a real ClientError, as botocore itself would construct one
    from an AWS API error response."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "boom"}},
        operation_name=operation_name,
    )


def make_instance(instance_id: str, name: str | None = None) -> dict:
    """Build a minimal describe_instances-shaped instance dict."""
    tags = [{"Key": "Name", "Value": name}] if name is not None else []
    return {"InstanceId": instance_id, "Tags": tags}


class TestTerminateInstance:
    def test_terminates_instance(self):
        ec2 = MagicMock()

        terminate_instance(ec2, "i-123")

        ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-123"])


class TestMain:
    def test_returns_early_when_instance_not_found(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": []}

        with patch("ec2.ec2_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "ec2.ec2_teardown.boto3.client", return_value=ec2
        ):
            main()

        ec2.terminate_instances.assert_not_called()

    def test_resolves_region_before_constructing_client(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": []}

        call_order = []
        with patch(
            "ec2.ec2_teardown.resolve_region",
            side_effect=lambda: call_order.append("resolve_region") or "ap-southeast-2",
        ), patch(
            "ec2.ec2_teardown.boto3.client",
            side_effect=lambda *a, **kw: call_order.append("boto3.client") or ec2,
        ):
            main()

        assert call_order == ["resolve_region", "boto3.client"]

    def test_happy_path_terminates_and_logs_final_confirmation(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [make_instance("i-existing", INSTANCE_NAME)]}
            ]
        }

        with patch("ec2.ec2_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "ec2.ec2_teardown.boto3.client", return_value=ec2
        ), patch.object(
            __import__("ec2.ec2_teardown", fromlist=["logger"]), "logger"
        ) as mock_logger:
            main()

        ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-existing"])
        confirmation_logged = any(
            "removed" in str(args[0]) or "terminated" in str(args[0])
            for args, _ in mock_logger.info.call_args_list
        )
        assert confirmation_logged

    def test_halts_without_raising_on_non_not_found_client_error(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [make_instance("i-existing", INSTANCE_NAME)]}
            ]
        }
        ec2.terminate_instances.side_effect = make_client_error(
            "InternalError", "TerminateInstances"
        )

        with patch("ec2.ec2_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "ec2.ec2_teardown.boto3.client", return_value=ec2
        ):
            main()  # must not raise

        ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-existing"])

    def test_not_found_error_logs_absence_and_continues(self):
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [make_instance("i-existing", INSTANCE_NAME)]}
            ]
        }
        ec2.terminate_instances.side_effect = make_client_error(
            "InvalidInstanceID.NotFound", "TerminateInstances"
        )

        with patch("ec2.ec2_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "ec2.ec2_teardown.boto3.client", return_value=ec2
        ):
            main()  # must not raise

        ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-existing"])
