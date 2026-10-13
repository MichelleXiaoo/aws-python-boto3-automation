"""Unit tests for rds/rds_provisioner.py.

Covers ensure_vpc/ensure_subnet's 0-match-creates/1-match-reuses/2+-match-
ambiguous behavior (mirroring the VPC domain's test style),
ensure_db_subnet_group's create-when-absent/skip-when-present behavior,
ensure_db_cluster's create+instance+wait-via-get_waiter behavior (and that
no manual polling loop exists), and main()'s overall sequencing --
including the critical ordering requirement that require_env is called
before resolve_region or any boto3.client(...) construction.

The boto3 EC2/RDS clients are mocked throughout via unittest.mock; no real
AWS calls are made. Environment variables are controlled via monkeypatch.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import rds.rds_provisioner as rds_provisioner
from common.exceptions import AmbiguousResourceError, MissingSecretError, ResourceCreationError
from rds.rds_provisioner import (
    AZ_SUFFIXES,
    DB_CLUSTER_ID,
    DB_PASSWORD_ENV_VAR,
    DB_SUBNET_GROUP,
    INSTANCE_ID,
    SUBNET_CIDRS,
    USERNAME,
    VPC_CIDR,
    VPC_NAME,
    RdsResources,
    ensure_db_cluster,
    ensure_db_subnet_group,
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


class _DBSubnetGroupNotFoundFault(ClientError):
    """Stand-in for botocore's dynamically generated
    ``rds.exceptions.DBSubnetGroupNotFoundFault``."""

    def __init__(self):
        super().__init__(
            error_response={
                "Error": {"Code": "DBSubnetGroupNotFoundFault", "Message": "not found"}
            },
            operation_name="DescribeDBSubnetGroups",
        )


class _DBClusterNotFoundFault(ClientError):
    """Stand-in for botocore's dynamically generated
    ``rds.exceptions.DBClusterNotFoundFault``."""

    def __init__(self):
        super().__init__(
            error_response={
                "Error": {"Code": "DBClusterNotFoundFault", "Message": "not found"}
            },
            operation_name="DescribeDBClusters",
        )


def make_rds_client() -> MagicMock:
    """Build a MagicMock RDS client with real exception classes attached
    to ``.exceptions``, so ``except rds.exceptions.XyzFault`` works the way
    it does against a real boto3 client."""
    rds = MagicMock()
    rds.exceptions.DBSubnetGroupNotFoundFault = _DBSubnetGroupNotFoundFault
    rds.exceptions.DBClusterNotFoundFault = _DBClusterNotFoundFault
    return rds


class TestEnsureVpc:
    """Mirrors vpc.vpc_provisioner's ensure_vpc test style, since
    rds.rds_provisioner.ensure_vpc duplicates that behavior for a
    separate, distinctly named VPC."""

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

    def test_uses_a_distinct_name_from_the_vpc_domain(self):
        """This script must provision its own, separately named VPC
        rather than reusing the VPC domain's 'vpc-boto3' (see the module
        docstring's discussion of why the domains are kept independent)."""
        assert VPC_NAME != "vpc-boto3"


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


class TestEnsureDbSubnetGroup:
    def test_creates_when_absent(self):
        rds = make_rds_client()
        rds.describe_db_subnet_groups.side_effect = _DBSubnetGroupNotFoundFault()
        rds.create_db_subnet_group.return_value = {
            "DBSubnetGroup": {"DBSubnetGroupArn": "arn:aws:rds:::subgrp/new"}
        }

        ensure_db_subnet_group(rds, DB_SUBNET_GROUP, ["subnet-1", "subnet-2", "subnet-3"])

        rds.create_db_subnet_group.assert_called_once()
        call_kwargs = rds.create_db_subnet_group.call_args.kwargs
        assert call_kwargs["DBSubnetGroupName"] == DB_SUBNET_GROUP
        assert call_kwargs["SubnetIds"] == ["subnet-1", "subnet-2", "subnet-3"]

    def test_skips_when_present(self):
        rds = make_rds_client()
        rds.describe_db_subnet_groups.return_value = {
            "DBSubnetGroups": [{"DBSubnetGroupName": DB_SUBNET_GROUP}]
        }

        ensure_db_subnet_group(rds, DB_SUBNET_GROUP, ["subnet-1", "subnet-2", "subnet-3"])

        rds.create_db_subnet_group.assert_not_called()

    def test_raises_resource_creation_error_when_create_fails(self):
        rds = make_rds_client()
        rds.describe_db_subnet_groups.side_effect = _DBSubnetGroupNotFoundFault()
        rds.create_db_subnet_group.side_effect = make_client_error("CreateDBSubnetGroup")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_db_subnet_group(rds, DB_SUBNET_GROUP, ["subnet-1"])

        assert exc_info.value.resource_type == "db_subnet_group"


class TestEnsureDbCluster:
    def test_creates_instance_and_waits_when_absent(self):
        rds = make_rds_client()
        rds.describe_db_clusters.side_effect = _DBClusterNotFoundFault()
        waiter = MagicMock()
        rds.get_waiter.return_value = waiter

        ensure_db_cluster(rds, DB_CLUSTER_ID, DB_SUBNET_GROUP, "s3cr3t-password")

        rds.create_db_cluster.assert_called_once()
        create_cluster_kwargs = rds.create_db_cluster.call_args.kwargs
        assert create_cluster_kwargs["DBClusterIdentifier"] == DB_CLUSTER_ID
        assert create_cluster_kwargs["MasterUsername"] == USERNAME
        assert create_cluster_kwargs["MasterUserPassword"] == "s3cr3t-password"
        assert create_cluster_kwargs["DBSubnetGroupName"] == DB_SUBNET_GROUP
        assert create_cluster_kwargs["Engine"] == "aurora-mysql"

        rds.create_db_instance.assert_called_once()
        create_instance_kwargs = rds.create_db_instance.call_args.kwargs
        assert create_instance_kwargs["DBInstanceIdentifier"] == INSTANCE_ID
        assert create_instance_kwargs["DBClusterIdentifier"] == DB_CLUSTER_ID
        assert create_instance_kwargs["DBInstanceClass"] == "db.serverless"

        rds.get_waiter.assert_called_once_with("db_cluster_available")
        waiter.wait.assert_called_once_with(DBClusterIdentifier=DB_CLUSTER_ID)

    def test_skips_creation_and_wait_when_already_exists(self):
        rds = make_rds_client()
        rds.describe_db_clusters.return_value = {
            "DBClusters": [{"DBClusterIdentifier": DB_CLUSTER_ID}]
        }

        ensure_db_cluster(rds, DB_CLUSTER_ID, DB_SUBNET_GROUP, "s3cr3t-password")

        rds.create_db_cluster.assert_not_called()
        rds.create_db_instance.assert_not_called()
        rds.get_waiter.assert_not_called()

    def test_raises_resource_creation_error_when_create_cluster_fails(self):
        rds = make_rds_client()
        rds.describe_db_clusters.side_effect = _DBClusterNotFoundFault()
        rds.create_db_cluster.side_effect = make_client_error("CreateDBCluster")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_db_cluster(rds, DB_CLUSTER_ID, DB_SUBNET_GROUP, "s3cr3t-password")

        assert exc_info.value.resource_type == "db_cluster"
        rds.create_db_instance.assert_not_called()
        rds.get_waiter.assert_not_called()

    def test_raises_resource_creation_error_when_create_instance_fails(self):
        rds = make_rds_client()
        rds.describe_db_clusters.side_effect = _DBClusterNotFoundFault()
        rds.create_db_instance.side_effect = make_client_error("CreateDBInstance")

        with pytest.raises(ResourceCreationError) as exc_info:
            ensure_db_cluster(rds, DB_CLUSTER_ID, DB_SUBNET_GROUP, "s3cr3t-password")

        assert exc_info.value.resource_type == "db_cluster"
        rds.get_waiter.assert_not_called()

    def test_implementation_contains_no_manual_polling_loop(self):
        """Guards against regressing back to the original script's
        'while True: time.sleep(40)' loop: the source of ensure_db_cluster
        must not reference time.sleep or a bare while loop, and must call
        get_waiter."""
        source = inspect.getsource(ensure_db_cluster)

        assert "sleep(" not in source
        assert "while True" not in source
        assert "get_waiter" in source


class TestMain:
    def test_require_env_called_before_resolve_region_or_any_boto3_client(self, monkeypatch):
        """Critical ordering requirement: require_env() for the
        master-password env var must be called first, before
        resolve_region() or any boto3.client(...) construction."""
        monkeypatch.setenv(DB_PASSWORD_ENV_VAR, "s3cr3t-password")

        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-existing"}]}
        ec2.describe_subnets.side_effect = [
            {"Subnets": [{"SubnetId": f"subnet-existing-{i}"}]} for i in range(3)
        ]
        rds = make_rds_client()
        rds.describe_db_subnet_groups.return_value = {
            "DBSubnetGroups": [{"DBSubnetGroupName": DB_SUBNET_GROUP}]
        }
        rds.describe_db_clusters.return_value = {
            "DBClusters": [{"DBClusterIdentifier": DB_CLUSTER_ID}]
        }

        call_order = []
        real_require_env = rds_provisioner.require_env

        def tracking_require_env(var_name):
            call_order.append("require_env")
            return real_require_env(var_name)

        def tracking_resolve_region():
            call_order.append("resolve_region")
            return "ap-southeast-2"

        def tracking_boto3_client(service_name, **kwargs):
            call_order.append(f"boto3.client:{service_name}")
            return ec2 if service_name == "ec2" else rds

        with patch.object(
            rds_provisioner, "require_env", side_effect=tracking_require_env
        ), patch.object(
            rds_provisioner, "resolve_region", side_effect=tracking_resolve_region
        ), patch.object(
            rds_provisioner.boto3, "client", side_effect=tracking_boto3_client
        ):
            main()

        assert call_order[0] == "require_env"
        assert call_order.index("require_env") < call_order.index("resolve_region")
        assert call_order.index("resolve_region") < call_order.index("boto3.client:ec2")

    def test_missing_secret_error_propagates_with_no_aws_call_made(self, monkeypatch):
        """If the master-password env var is unset, require_env raises
        MissingSecretError before resolve_region or any boto3 client is
        constructed, and no AWS resources are created."""
        monkeypatch.delenv(DB_PASSWORD_ENV_VAR, raising=False)

        with patch.object(
            rds_provisioner, "resolve_region"
        ) as mock_resolve_region, patch.object(
            rds_provisioner.boto3, "client"
        ) as mock_client:
            with pytest.raises(MissingSecretError, match=DB_PASSWORD_ENV_VAR):
                main()

            mock_resolve_region.assert_not_called()
            mock_client.assert_not_called()

    def test_main_sequences_calls_and_returns_rds_resources(self, monkeypatch):
        monkeypatch.setenv(DB_PASSWORD_ENV_VAR, "s3cr3t-password")

        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": []}
        ec2.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-new"}}
        ec2.describe_subnets.return_value = {"Subnets": []}
        ec2.create_subnet.side_effect = [
            {"Subnet": {"SubnetId": f"subnet-new-{i}"}} for i in range(len(SUBNET_CIDRS))
        ]

        rds = make_rds_client()
        rds.describe_db_subnet_groups.side_effect = _DBSubnetGroupNotFoundFault()
        rds.create_db_subnet_group.return_value = {
            "DBSubnetGroup": {"DBSubnetGroupArn": "arn:aws:rds:::subgrp/new"}
        }
        rds.describe_db_clusters.side_effect = _DBClusterNotFoundFault()

        def client_factory(service_name, **kwargs):
            assert kwargs["region_name"] == "ap-southeast-2"
            return ec2 if service_name == "ec2" else rds

        with patch.object(
            rds_provisioner, "resolve_region", return_value="ap-southeast-2"
        ), patch.object(rds_provisioner.boto3, "client", side_effect=client_factory):
            result = main()

        assert result == RdsResources(
            vpc_id="vpc-new",
            subnet_ids=["subnet-new-0", "subnet-new-1", "subnet-new-2"],
            db_subnet_group=DB_SUBNET_GROUP,
            cluster_id=DB_CLUSTER_ID,
            instance_id=INSTANCE_ID,
        )

        # Subnets created in distinct AZs derived from the resolved region.
        azs_used = [
            call.kwargs["AvailabilityZone"] for call in ec2.create_subnet.call_args_list
        ]
        assert azs_used == [f"ap-southeast-2{suffix}" for suffix in AZ_SUFFIXES]

        rds.create_db_subnet_group.assert_called_once()
        rds.create_db_cluster.assert_called_once()
        rds.create_db_instance.assert_called_once()
        rds.get_waiter.return_value.wait.assert_called_once_with(
            DBClusterIdentifier=DB_CLUSTER_ID
        )
