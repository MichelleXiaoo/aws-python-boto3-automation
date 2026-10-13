"""Unit tests for vpc/vpc_provisioner.py.

Covers each ensure_* function's 0-match-creates and 1-match-reuses
behavior, the 2+-match AmbiguousResourceError case, the create-failure
ResourceCreationError-wrapping case, and main()'s overall sequencing
producing a correct VpcResources -- these are the SMOKE/EXAMPLE scenarios;
the full Hypothesis property-test suite is intentionally out of scope
here.

The boto3 EC2 client is mocked throughout via unittest.mock; no real AWS
calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from common.exceptions import AmbiguousResourceError, ResourceCreationError
from vpc.vpc_provisioner import (
    AZ_SUFFIXES,
    IGW_NAME,
    RT_NAME,
    SUBNET_CIDRS,
    VPC_CIDR,
    VPC_NAME,
    VpcResources,
    ensure_association,
    ensure_igw,
    ensure_route_table,
    ensure_subnet,
    ensure_vpc,
    main,
)


def make_client_error(operation_name: str = "CreateThing") -> ClientError:
    """Build a real ClientError, as botocore itself would construct one
    from an AWS API error response."""
    return ClientError(
        error_response={"Error": {"Code": "InternalError", "Message": "boom"}},
        operation_name=operation_name,
    )


class TestEnsureVpc:
    def test_creates_when_no_match(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": []}
        ec2.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-new"}}

        vpc_id = ensure_vpc(ec2, VPC_NAME, VPC_CIDR)

        assert vpc_id == "vpc-new"
        ec2.create_vpc.assert_called_once()
        call_kwargs = ec2.create_vpc.call_args.kwargs
        assert call_kwargs["CidrBlock"] == VPC_CIDR
        assert call_kwargs["TagSpecifications"] == [
            {"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": VPC_NAME}]}
        ]

    def test_reuses_when_one_match(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-existing"}]}

        vpc_id = ensure_vpc(ec2, VPC_NAME, VPC_CIDR)

        assert vpc_id == "vpc-existing"
        ec2.create_vpc.assert_not_called()

    def test_raises_ambiguous_resource_error_on_two_matches(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {
            "Vpcs": [{"VpcId": "vpc-aaa"}, {"VpcId": "vpc-bbb"}]
        }

        with pytest.raises(AmbiguousResourceError) as exc_info:
            ensure_vpc(ec2, VPC_NAME, VPC_CIDR)

        assert exc_info.value.resource_type == "vpc"
        assert exc_info.value.matching_ids == ["vpc-aaa", "vpc-bbb"]
        ec2.create_vpc.assert_not_called()

    def test_raises_resource_creation_error_when_create_fails(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": []}
        ec2.create_vpc.side_effect = make_client_error("CreateVpc")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_vpc(ec2, VPC_NAME, VPC_CIDR)

        assert exc_info.value.resource_type == "vpc"
        assert isinstance(exc_info.value.cause, ClientError)


class TestEnsureIgw:
    def test_creates_and_attaches_when_no_match(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {"InternetGateways": []}
        ec2.create_internet_gateway.return_value = {
            "InternetGateway": {"InternetGatewayId": "igw-new"}
        }

        igw_id = ensure_igw(ec2, IGW_NAME, "vpc-123")

        assert igw_id == "igw-new"
        ec2.create_internet_gateway.assert_called_once()
        tag_specs = ec2.create_internet_gateway.call_args.kwargs["TagSpecifications"]
        assert tag_specs == [
            {"ResourceType": "internet-gateway", "Tags": [{"Key": "Name", "Value": IGW_NAME}]}
        ]
        ec2.attach_internet_gateway.assert_called_once_with(
            InternetGatewayId="igw-new", VpcId="vpc-123"
        )

    def test_reuses_when_one_match(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {
            "InternetGateways": [{"InternetGatewayId": "igw-existing"}]
        }

        igw_id = ensure_igw(ec2, IGW_NAME, "vpc-123")

        assert igw_id == "igw-existing"
        ec2.create_internet_gateway.assert_not_called()

    def test_raises_ambiguous_resource_error_on_two_matches(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {
            "InternetGateways": [
                {"InternetGatewayId": "igw-aaa"},
                {"InternetGatewayId": "igw-bbb"},
            ]
        }

        with pytest.raises(AmbiguousResourceError) as exc_info:
            ensure_igw(ec2, IGW_NAME, "vpc-123")

        assert exc_info.value.resource_type == "internet_gateway"
        assert exc_info.value.matching_ids == ["igw-aaa", "igw-bbb"]
        ec2.create_internet_gateway.assert_not_called()

    def test_raises_resource_creation_error_when_create_fails(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {"InternetGateways": []}
        ec2.create_internet_gateway.side_effect = make_client_error("CreateInternetGateway")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_igw(ec2, IGW_NAME, "vpc-123")

        assert exc_info.value.resource_type == "internet_gateway"

    def test_raises_resource_creation_error_when_attach_fails(self):
        ec2 = MagicMock()
        ec2.describe_internet_gateways.return_value = {"InternetGateways": []}
        ec2.create_internet_gateway.return_value = {
            "InternetGateway": {"InternetGatewayId": "igw-new"}
        }
        ec2.attach_internet_gateway.side_effect = make_client_error("AttachInternetGateway")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_igw(ec2, IGW_NAME, "vpc-123")

        assert exc_info.value.resource_type == "internet_gateway"


class TestEnsureRouteTable:
    def test_creates_with_public_route_when_no_match(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {"RouteTables": []}
        ec2.create_route_table.return_value = {"RouteTable": {"RouteTableId": "rtb-new"}}

        rt_id = ensure_route_table(ec2, RT_NAME, "vpc-123", "igw-456")

        assert rt_id == "rtb-new"
        ec2.create_route_table.assert_called_once()
        call_kwargs = ec2.create_route_table.call_args.kwargs
        assert call_kwargs["VpcId"] == "vpc-123"
        assert call_kwargs["TagSpecifications"] == [
            {"ResourceType": "route-table", "Tags": [{"Key": "Name", "Value": RT_NAME}]}
        ]
        ec2.create_route.assert_called_once_with(
            RouteTableId="rtb-new", DestinationCidrBlock="0.0.0.0/0", GatewayId="igw-456"
        )

    def test_reuses_when_one_match(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {
            "RouteTables": [{"RouteTableId": "rtb-existing"}]
        }

        rt_id = ensure_route_table(ec2, RT_NAME, "vpc-123", "igw-456")

        assert rt_id == "rtb-existing"
        ec2.create_route_table.assert_not_called()

    def test_raises_ambiguous_resource_error_on_two_matches(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {
            "RouteTables": [{"RouteTableId": "rtb-aaa"}, {"RouteTableId": "rtb-bbb"}]
        }

        with pytest.raises(AmbiguousResourceError) as exc_info:
            ensure_route_table(ec2, RT_NAME, "vpc-123", "igw-456")

        assert exc_info.value.resource_type == "route_table"
        assert exc_info.value.matching_ids == ["rtb-aaa", "rtb-bbb"]
        ec2.create_route_table.assert_not_called()

    def test_raises_resource_creation_error_when_create_fails(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {"RouteTables": []}
        ec2.create_route_table.side_effect = make_client_error("CreateRouteTable")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_route_table(ec2, RT_NAME, "vpc-123", "igw-456")

        assert exc_info.value.resource_type == "route_table"

    def test_raises_resource_creation_error_when_create_route_fails(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {"RouteTables": []}
        ec2.create_route_table.return_value = {"RouteTable": {"RouteTableId": "rtb-new"}}
        ec2.create_route.side_effect = make_client_error("CreateRoute")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_route_table(ec2, RT_NAME, "vpc-123", "igw-456")

        assert exc_info.value.resource_type == "route_table"


class TestEnsureSubnet:
    def test_creates_when_no_match(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {"Subnets": []}
        ec2.create_subnet.return_value = {"Subnet": {"SubnetId": "subnet-new"}}

        subnet_id = ensure_subnet(ec2, "vpc-123", "10.0.1.0/24", "ap-southeast-2a")

        assert subnet_id == "subnet-new"
        ec2.create_subnet.assert_called_once()
        call_kwargs = ec2.create_subnet.call_args.kwargs
        assert call_kwargs["VpcId"] == "vpc-123"
        assert call_kwargs["CidrBlock"] == "10.0.1.0/24"
        assert call_kwargs["AvailabilityZone"] == "ap-southeast-2a"
        assert call_kwargs["TagSpecifications"][0]["ResourceType"] == "subnet"

    def test_reuses_when_one_match(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {"Subnets": [{"SubnetId": "subnet-existing"}]}

        subnet_id = ensure_subnet(ec2, "vpc-123", "10.0.1.0/24", "ap-southeast-2a")

        assert subnet_id == "subnet-existing"
        ec2.create_subnet.assert_not_called()

    def test_raises_ambiguous_resource_error_on_two_matches(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-aaa"}, {"SubnetId": "subnet-bbb"}]
        }

        with pytest.raises(AmbiguousResourceError) as exc_info:
            ensure_subnet(ec2, "vpc-123", "10.0.1.0/24", "ap-southeast-2a")

        assert exc_info.value.resource_type == "subnet"
        assert exc_info.value.matching_ids == ["subnet-aaa", "subnet-bbb"]
        ec2.create_subnet.assert_not_called()

    def test_raises_resource_creation_error_when_create_fails(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {"Subnets": []}
        ec2.create_subnet.side_effect = make_client_error("CreateSubnet")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_subnet(ec2, "vpc-123", "10.0.1.0/24", "ap-southeast-2a")

        assert exc_info.value.resource_type == "subnet"


class TestEnsureAssociation:
    def test_associates_when_not_already_associated(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {
            "RouteTables": [{"Associations": [{"SubnetId": "subnet-other"}]}]
        }

        ensure_association(ec2, "rtb-123", "subnet-456")

        ec2.associate_route_table.assert_called_once_with(
            RouteTableId="rtb-123", SubnetId="subnet-456"
        )

    def test_skips_association_when_already_associated(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {
            "RouteTables": [{"Associations": [{"SubnetId": "subnet-456"}]}]
        }

        ensure_association(ec2, "rtb-123", "subnet-456")

        ec2.associate_route_table.assert_not_called()

    def test_raises_resource_creation_error_when_associate_fails(self):
        ec2 = MagicMock()
        ec2.describe_route_tables.return_value = {"RouteTables": [{"Associations": []}]}
        ec2.associate_route_table.side_effect = make_client_error("AssociateRouteTable")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_association(ec2, "rtb-123", "subnet-456")

        assert exc_info.value.resource_type == "route_table_association"


class TestMain:
    def _configure_fresh_create_mocks(self, ec2):
        """Configure an all-mock EC2 client such that every describe_*
        call reports zero matches and every create_* call succeeds,
        simulating a from-scratch provisioning run."""
        ec2.describe_vpcs.return_value = {"Vpcs": []}
        ec2.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-new"}}

        ec2.describe_internet_gateways.return_value = {"InternetGateways": []}
        ec2.create_internet_gateway.return_value = {
            "InternetGateway": {"InternetGatewayId": "igw-new"}
        }

        ec2.describe_route_tables.return_value = {"RouteTables": []}
        ec2.create_route_table.return_value = {"RouteTable": {"RouteTableId": "rtb-new"}}

        ec2.describe_subnets.return_value = {"Subnets": []}
        ec2.create_subnet.side_effect = [
            {"Subnet": {"SubnetId": f"subnet-new-{i}"}} for i in range(len(SUBNET_CIDRS))
        ]

    def test_main_sequences_calls_and_returns_vpc_resources(self):
        ec2 = MagicMock()
        self._configure_fresh_create_mocks(ec2)
        # ensure_association's describe_route_tables call for each subnet
        # reports no existing associations, so main() always associates.
        ec2.describe_route_tables.side_effect = [
            {"RouteTables": []},  # ensure_route_table's own describe
            {"RouteTables": [{"Associations": []}]},
            {"RouteTables": [{"Associations": []}]},
            {"RouteTables": [{"Associations": []}]},
        ]

        with patch("vpc.vpc_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_provisioner.boto3.client", return_value=ec2
        ) as mock_client:
            result = main()

        mock_client.assert_called_once_with("ec2", region_name="ap-southeast-2")
        assert result == VpcResources(
            vpc_id="vpc-new",
            igw_id="igw-new",
            route_table_id="rtb-new",
            subnet_ids=["subnet-new-0", "subnet-new-1", "subnet-new-2"],
        )

        # Three subnets, three associations.
        assert ec2.create_subnet.call_count == 3
        assert ec2.associate_route_table.call_count == 3

        # Subnets were created in distinct AZs derived from the resolved
        # region, matching AZ_SUFFIXES, in order.
        azs_used = [
            call.kwargs["AvailabilityZone"] for call in ec2.create_subnet.call_args_list
        ]
        assert azs_used == [f"ap-southeast-2{suffix}" for suffix in AZ_SUFFIXES]

    def test_main_resolves_region_before_constructing_client(self):
        """resolve_region() must be called before boto3.client(...), per
        the design's region-resolution flow."""
        ec2 = MagicMock()
        self._configure_fresh_create_mocks(ec2)
        ec2.describe_route_tables.side_effect = [
            {"RouteTables": []},
            {"RouteTables": [{"Associations": []}]},
            {"RouteTables": [{"Associations": []}]},
            {"RouteTables": [{"Associations": []}]},
        ]

        call_order = []
        with patch(
            "vpc.vpc_provisioner.resolve_region",
            side_effect=lambda: call_order.append("resolve_region") or "ap-southeast-2",
        ), patch(
            "vpc.vpc_provisioner.boto3.client",
            side_effect=lambda *a, **kw: call_order.append("boto3.client") or ec2,
        ):
            main()

        assert call_order == ["resolve_region", "boto3.client"]

    def test_main_reuses_everything_on_second_run(self):
        """When every resource already exists, main() issues no create or
        associate calls and returns the existing identifiers."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-existing"}]}
        ec2.describe_internet_gateways.return_value = {
            "InternetGateways": [{"InternetGatewayId": "igw-existing"}]
        }
        ec2.describe_route_tables.return_value = {
            "RouteTables": [{"RouteTableId": "rtb-existing", "Associations": [
                {"SubnetId": "subnet-existing-0"},
                {"SubnetId": "subnet-existing-1"},
                {"SubnetId": "subnet-existing-2"},
            ]}]
        }
        ec2.describe_subnets.side_effect = [
            {"Subnets": [{"SubnetId": f"subnet-existing-{i}"}]} for i in range(3)
        ]

        with patch("vpc.vpc_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_provisioner.boto3.client", return_value=ec2
        ):
            result = main()

        assert result == VpcResources(
            vpc_id="vpc-existing",
            igw_id="igw-existing",
            route_table_id="rtb-existing",
            subnet_ids=["subnet-existing-0", "subnet-existing-1", "subnet-existing-2"],
        )
        ec2.create_vpc.assert_not_called()
        ec2.create_internet_gateway.assert_not_called()
        ec2.create_route_table.assert_not_called()
        ec2.create_subnet.assert_not_called()
        ec2.associate_route_table.assert_not_called()

    def test_main_halts_on_ambiguous_vpc_before_creating_anything_else(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {
            "Vpcs": [{"VpcId": "vpc-aaa"}, {"VpcId": "vpc-bbb"}]
        }

        with patch("vpc.vpc_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_provisioner.boto3.client", return_value=ec2
        ):
            with pytest.raises(AmbiguousResourceError):
                main()

        ec2.create_vpc.assert_not_called()
        ec2.describe_internet_gateways.assert_not_called()

    def test_main_halts_on_creation_failure_before_later_steps(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": []}
        ec2.create_vpc.side_effect = make_client_error("CreateVpc")

        with patch("vpc.vpc_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "vpc.vpc_provisioner.boto3.client", return_value=ec2
        ):
            with pytest.raises(ResourceCreationError) as exc_info:
                main()

        assert exc_info.value.resource_type == "vpc"
        ec2.describe_internet_gateways.assert_not_called()
        ec2.create_internet_gateway.assert_not_called()
