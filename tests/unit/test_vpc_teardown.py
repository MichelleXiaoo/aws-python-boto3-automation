"""Unit tests for vpc/vpc_teardown.py.

Covers each delete_* function individually (including skipping the main
route table and disassociating non-main ones, and detach-then-delete order
for the internet gateway), main()'s "VPC not found" early-return path, and
main()'s full sequence via the real common.teardown.run_step -- both the
all-steps-succeed happy path (final confirmation logged) and a halting
failure partway through (later steps never run). These are the
SMOKE/EXAMPLE scenarios; the full Hypothesis property-test suite is
intentionally out of scope here.

The boto3 EC2 client is mocked throughout via unittest.mock; no real AWS
calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from botocore.exceptions import ClientError

from vpc.vpc_teardown import (
    delete_igw,
    delete_route_tables,
    delete_subnets,
    delete_vpc,
    main,
)
from vpc.vpc_provisioner import IGW_NAME, VPC_NAME


def make_client_error(code: str, operation_name: str = "DeleteThing") -> ClientError:
    """Build a real ClientError with a controlled Error.Code, as botocore
    itself would construct one from an AWS API error response."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "boom"}},
        operation_name=operation_name,
    )


class TestDeleteSubnets:
    def test_deletes_every_subnet_in_vpc(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {
            "Subnets": [
                {"SubnetId": "subnet-1", "CidrBlock": "10.0.1.0/24"},
                {"SubnetId": "subnet-2", "CidrBlock": "10.0.2.0/24"},
            ]
        }

        delete_subnets(ec2, "vpc-123")

        ec2.describe_subnets.assert_called_once_with(
            Filters=[{"Name": "vpc-id", "Values": ["vpc-123"]}]
        )
        ec2.delete_subnet.assert_has_calls(
            [call(SubnetId="subnet-1"), call(SubnetId="subnet-2")]
        )
        assert ec2.delete_subnet.call_count == 2

    def test_no_subnets_deletes_nothing(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {"Subnets": []}

        delete_subnets(ec2, "vpc-123")

        ec2.delete_subnet.assert_not_called()


class TestDeleteRouteTables:
    def test_skips_main_route_table(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {
            "RouteTables": [
                {"RouteTableId": "rtb-main", "Associations": [{"Main": True}]},
            ]
        }

        delete_route_tables(ec2, "vpc-123")

        ec2.delete_route_table.assert_not_called()
        ec2.disassociate_route_table.assert_not_called()

    def test_disassociates_and_deletes_non_main_route_table(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {
            "RouteTables": [
                {
                    "RouteTableId": "rtb-custom",
                    "Associations": [
                        {"Main": False, "RouteTableAssociationId": "rtbassoc-1"},
                        {"Main": False, "RouteTableAssociationId": "rtbassoc-2"},
                    ],
                },
            ]
        }

        delete_route_tables(ec2, "vpc-123")

        ec2.disassociate_route_table.assert_has_calls(
            [call(AssociationId="rtbassoc-1"), call(AssociationId="rtbassoc-2")]
        )
        ec2.delete_route_table.assert_called_once_with(RouteTableId="rtb-custom")

    def test_mixed_main_and_non_main_route_tables(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {
            "RouteTables": [
                {"RouteTableId": "rtb-main", "Associations": [{"Main": True}]},
                {
                    "RouteTableId": "rtb-custom",
                    "Associations": [
                        {"Main": False, "RouteTableAssociationId": "rtbassoc-1"}
                    ],
                },
            ]
        }

        delete_route_tables(ec2, "vpc-123")

        ec2.delete_route_table.assert_called_once_with(RouteTableId="rtb-custom")
        ec2.disassociate_route_table.assert_called_once_with(AssociationId="rtbassoc-1")

    def test_no_route_tables_deletes_nothing(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {"RouteTables": []}

        delete_route_tables(ec2, "vpc-123")

        ec2.delete_route_table.assert_not_called()
        ec2.disassociate_route_table.assert_not_called()


class TestDeleteIgw:
    def test_detaches_before_deleting(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {
            "InternetGateways": [
                {
                    "InternetGatewayId": "igw-123",
                    "Attachments": [{"VpcId": "vpc-123"}],
                }
            ]
        }

        delete_igw(ec2, IGW_NAME, "vpc-123")

        ec2.describe_internet_gateways.assert_called_once_with(
            Filters=[{"Name": "tag:Name", "Values": [IGW_NAME]}]
        )
        ec2.detach_internet_gateway.assert_called_once_with(
            InternetGatewayId="igw-123", VpcId="vpc-123"
        )
        ec2.delete_internet_gateway.assert_called_once_with(InternetGatewayId="igw-123")

        # Detach must happen before delete.
        detach_index = ec2.method_calls.index(
            call.detach_internet_gateway(InternetGatewayId="igw-123", VpcId="vpc-123")
        )
        delete_index = ec2.method_calls.index(
            call.delete_internet_gateway(InternetGatewayId="igw-123")
        )
        assert detach_index < delete_index

    def test_detaches_from_every_attached_vpc(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {
            "InternetGateways": [
                {
                    "InternetGatewayId": "igw-123",
                    "Attachments": [{"VpcId": "vpc-aaa"}, {"VpcId": "vpc-bbb"}],
                }
            ]
        }

        delete_igw(ec2, IGW_NAME, "vpc-aaa")

        ec2.detach_internet_gateway.assert_has_calls(
            [
                call(InternetGatewayId="igw-123", VpcId="vpc-aaa"),
                call(InternetGatewayId="igw-123", VpcId="vpc-bbb"),
            ]
        )
        ec2.delete_internet_gateway.assert_called_once_with(InternetGatewayId="igw-123")

    def test_no_attachments_only_deletes(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {
            "InternetGateways": [{"InternetGatewayId": "igw-123", "Attachments": []}]
        }

        delete_igw(ec2, IGW_NAME, "vpc-123")

        ec2.detach_internet_gateway.assert_not_called()
        ec2.delete_internet_gateway.assert_called_once_with(InternetGatewayId="igw-123")

    def test_no_matching_igw_deletes_nothing(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {"InternetGateways": []}

        delete_igw(ec2, IGW_NAME, "vpc-123")

        ec2.detach_internet_gateway.assert_not_called()
        ec2.delete_internet_gateway.assert_not_called()


class TestDeleteVpc:
    def test_deletes_vpc(self):
        ec2 = MagicMock()

        delete_vpc(ec2, "vpc-123")

        ec2.delete_vpc.assert_called_once_with(VpcId="vpc-123")


class TestMain:
    def test_returns_early_when_vpc_not_found(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": []}

        with patch("vpc.vpc_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_teardown.boto3.client", return_value=ec2
        ):
            main()

        ec2.describe_vpcs.assert_called_once_with(
            Filters=[{"Name": "tag:Name", "Values": [VPC_NAME]}]
        )
        ec2.describe_subnets.assert_not_called()
        ec2.describe_route_tables.assert_not_called()
        ec2.describe_internet_gateways.assert_not_called()
        ec2.delete_vpc.assert_not_called()

    def test_resolves_region_before_constructing_client(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": []}

        call_order = []
        with patch(
            "vpc.vpc_teardown.resolve_region",
            side_effect=lambda: call_order.append("resolve_region") or "ap-southeast-2",
        ), patch(
            "vpc.vpc_teardown.boto3.client",
            side_effect=lambda *a, **kw: call_order.append("boto3.client") or ec2,
        ):
            main()

        assert call_order == ["resolve_region", "boto3.client"]

    def test_happy_path_runs_all_steps_and_logs_final_confirmation(self):
        """When every step succeeds, all four deletes run in order and a
        final confirmation is logged."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        ec2.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-1", "CidrBlock": "10.0.1.0/24"}]
        }
        ec2.describe_route_tables.return_value = {
            "RouteTables": [{"RouteTableId": "rtb-custom", "Associations": []}]
        }
        ec2.describe_internet_gateways.return_value = {
            "InternetGateways": [
                {"InternetGatewayId": "igw-123", "Attachments": [{"VpcId": "vpc-123"}]}
            ]
        }

        with patch("vpc.vpc_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_teardown.boto3.client", return_value=ec2
        ), patch.object(
            __import__("vpc.vpc_teardown", fromlist=["logger"]), "logger"
        ) as mock_logger:
            main()

        ec2.delete_subnet.assert_called_once_with(SubnetId="subnet-1")
        ec2.delete_route_table.assert_called_once_with(RouteTableId="rtb-custom")
        ec2.detach_internet_gateway.assert_called_once_with(
            InternetGatewayId="igw-123", VpcId="vpc-123"
        )
        ec2.delete_internet_gateway.assert_called_once_with(InternetGatewayId="igw-123")
        ec2.delete_vpc.assert_called_once_with(VpcId="vpc-123")

        # A final confirmation message was logged.
        confirmation_logged = any(
            "removed" in str(args[0]) for args, _ in mock_logger.info.call_args_list
        )
        assert confirmation_logged

    def test_halts_before_later_steps_on_non_not_found_client_error(self):
        """A non-not-found ClientError during route-table deletion halts
        before the internet gateway and VPC deletion steps run."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        ec2.describe_subnets.return_value = {"Subnets": []}
        ec2.describe_route_tables.return_value = {
            "RouteTables": [{"RouteTableId": "rtb-custom", "Associations": []}]
        }
        ec2.delete_route_table.side_effect = make_client_error(
            "DependencyViolation", "DeleteRouteTable"
        )

        with patch("vpc.vpc_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_teardown.boto3.client", return_value=ec2
        ):
            main()

        ec2.delete_route_table.assert_called_once_with(RouteTableId="rtb-custom")
        ec2.describe_internet_gateways.assert_not_called()
        ec2.delete_internet_gateway.assert_not_called()
        ec2.delete_vpc.assert_not_called()

    def test_not_found_error_logs_absence_and_continues_to_later_steps(self):
        """A not-found ClientError during subnet deletion is logged as an
        absence and the remaining steps still run."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        ec2.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-1", "CidrBlock": "10.0.1.0/24"}]
        }
        ec2.delete_subnet.side_effect = make_client_error(
            "InvalidSubnetID.NotFound", "DeleteSubnet"
        )
        ec2.describe_route_tables.return_value = {"RouteTables": []}
        ec2.describe_internet_gateways.return_value = {"InternetGateways": []}

        with patch("vpc.vpc_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_teardown.boto3.client", return_value=ec2
        ):
            main()

        ec2.delete_subnet.assert_called_once_with(SubnetId="subnet-1")
        # Teardown continued through route tables, IGW, and finally the VPC.
        ec2.describe_route_tables.assert_called_once()
        ec2.describe_internet_gateways.assert_called_once()
        ec2.delete_vpc.assert_called_once_with(VpcId="vpc-123")

    def test_run_step_mocked_to_control_continue_halt_sequencing(self):
        """Using a mocked common.teardown.run_step directly: all steps
        continue -> every delete_* step function is invoked once, in
        order."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}

        executed_steps = []

        def fake_run_step(logger, step_name, func):
            executed_steps.append(step_name)
            return True

        with patch("vpc.vpc_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_teardown.boto3.client", return_value=ec2
        ), patch("vpc.vpc_teardown.run_step", side_effect=fake_run_step):
            main()

        assert executed_steps == [
            "delete subnets",
            "delete route tables",
            "delete internet gateway",
            "delete VPC",
        ]

    def test_run_step_mocked_to_halt_after_second_step(self):
        """Using a mocked common.teardown.run_step directly: halting on the
        second step means the third and fourth step functions are never
        reached."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}

        executed_steps = []

        def fake_run_step(logger, step_name, func):
            executed_steps.append(step_name)
            return step_name != "delete route tables"

        with patch("vpc.vpc_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_teardown.boto3.client", return_value=ec2
        ), patch("vpc.vpc_teardown.run_step", side_effect=fake_run_step):
            main()

        assert executed_steps == ["delete subnets", "delete route tables"]
