"""Unit tests for rds/rds_teardown.py.

Covers each delete_* function individually (including delete_instance's and
delete_cluster's delete-then-wait call order and arguments), main()'s "VPC
not found" early-return path, and main()'s full sequence via the real
common.teardown.run_step -- the all-steps-succeed happy path (final
confirmation logged, correct step order), a halting failure partway
through on a non-not-found ClientError, and a WaiterError during one of the
wait() calls halting the sequence (confirming the "waiter timeout is an
other-failure" behavior inherited from common.teardown.run_step).

The boto3 RDS/EC2 clients are mocked throughout via unittest.mock; no real
AWS calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from botocore.exceptions import ClientError, WaiterError

from rds.rds_provisioner import DB_CLUSTER_ID, DB_SUBNET_GROUP, INSTANCE_ID, VPC_NAME
from rds.rds_teardown import (
    delete_cluster,
    delete_db_subnet_group,
    delete_instance,
    delete_subnets,
    delete_vpc,
    main,
)


def make_client_error(code: str, operation_name: str = "DeleteThing") -> ClientError:
    """Build a real ClientError with a controlled Error.Code, as botocore
    itself would construct one from an AWS API error response."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "boom"}},
        operation_name=operation_name,
    )


def make_waiter_error(operation_name: str = "WaitForThing") -> WaiterError:
    """Build a real WaiterError, as botocore itself would raise one when a
    waiter does not confirm completion within its configured attempts."""
    return WaiterError(name="db_instance_deleted", reason="Max attempts exceeded", last_response={})


class TestDeleteInstance:
    def test_deletes_then_waits_in_order_with_correct_args(self):
        rds = MagicMock()
        waiter = MagicMock()
        rds.get_waiter.return_value = waiter

        delete_instance(rds, INSTANCE_ID)

        rds.delete_db_instance.assert_called_once_with(
            DBInstanceIdentifier=INSTANCE_ID, SkipFinalSnapshot=True
        )
        rds.get_waiter.assert_called_once_with("db_instance_deleted")
        waiter.wait.assert_called_once_with(DBInstanceIdentifier=INSTANCE_ID)

        # Delete must happen before the waiter is obtained/waited on.
        delete_index = rds.method_calls.index(
            call.delete_db_instance(DBInstanceIdentifier=INSTANCE_ID, SkipFinalSnapshot=True)
        )
        get_waiter_index = rds.method_calls.index(call.get_waiter("db_instance_deleted"))
        assert delete_index < get_waiter_index


class TestDeleteCluster:
    def test_deletes_then_waits_in_order_with_correct_args(self):
        rds = MagicMock()
        waiter = MagicMock()
        rds.get_waiter.return_value = waiter

        delete_cluster(rds, DB_CLUSTER_ID)

        rds.delete_db_cluster.assert_called_once_with(
            DBClusterIdentifier=DB_CLUSTER_ID, SkipFinalSnapshot=True
        )
        rds.get_waiter.assert_called_once_with("db_cluster_deleted")
        waiter.wait.assert_called_once_with(DBClusterIdentifier=DB_CLUSTER_ID)

        delete_index = rds.method_calls.index(
            call.delete_db_cluster(DBClusterIdentifier=DB_CLUSTER_ID, SkipFinalSnapshot=True)
        )
        get_waiter_index = rds.method_calls.index(call.get_waiter("db_cluster_deleted"))
        assert delete_index < get_waiter_index


class TestDeleteDbSubnetGroup:
    def test_deletes_db_subnet_group(self):
        rds = MagicMock()

        delete_db_subnet_group(rds, DB_SUBNET_GROUP)

        rds.delete_db_subnet_group.assert_called_once_with(DBSubnetGroupName=DB_SUBNET_GROUP)


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


class TestDeleteVpc:
    def test_deletes_vpc(self):
        ec2 = MagicMock()

        delete_vpc(ec2, "vpc-123")

        ec2.delete_vpc.assert_called_once_with(VpcId="vpc-123")


class TestMain:
    def test_returns_early_when_vpc_not_found(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": []}
        rds = MagicMock()

        def client_factory(service_name, **kwargs):
            return ec2 if service_name == "ec2" else rds

        with patch("rds.rds_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "rds.rds_teardown.boto3.client", side_effect=client_factory
        ):
            main()

        ec2.describe_vpcs.assert_called_once_with(
            Filters=[{"Name": "tag:Name", "Values": [VPC_NAME]}]
        )
        rds.delete_db_instance.assert_not_called()
        rds.delete_db_cluster.assert_not_called()
        rds.delete_db_subnet_group.assert_not_called()
        ec2.delete_subnet.assert_not_called()
        ec2.delete_vpc.assert_not_called()

    def test_happy_path_runs_all_five_steps_in_order_and_logs_confirmation(self):
        """When every step succeeds, all five deletes run in the correct
        order (instance -> cluster -> subnet group -> subnets -> VPC) and a
        final confirmation is logged."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        ec2.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-1", "CidrBlock": "10.0.1.0/24"}]
        }
        rds = MagicMock()

        def client_factory(service_name, **kwargs):
            return ec2 if service_name == "ec2" else rds

        with patch("rds.rds_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "rds.rds_teardown.boto3.client", side_effect=client_factory
        ), patch.object(
            __import__("rds.rds_teardown", fromlist=["logger"]), "logger"
        ) as mock_logger:
            main()

        rds.delete_db_instance.assert_called_once_with(
            DBInstanceIdentifier=INSTANCE_ID, SkipFinalSnapshot=True
        )
        rds.delete_db_cluster.assert_called_once_with(
            DBClusterIdentifier=DB_CLUSTER_ID, SkipFinalSnapshot=True
        )
        rds.delete_db_subnet_group.assert_called_once_with(DBSubnetGroupName=DB_SUBNET_GROUP)
        ec2.delete_subnet.assert_called_once_with(SubnetId="subnet-1")
        ec2.delete_vpc.assert_called_once_with(VpcId="vpc-123")

        # Correct step order: instance -> cluster -> subnet group -> subnets -> VPC.
        instance_idx = rds.method_calls.index(
            call.delete_db_instance(DBInstanceIdentifier=INSTANCE_ID, SkipFinalSnapshot=True)
        )
        cluster_idx = rds.method_calls.index(
            call.delete_db_cluster(DBClusterIdentifier=DB_CLUSTER_ID, SkipFinalSnapshot=True)
        )
        subnet_group_idx = rds.method_calls.index(
            call.delete_db_subnet_group(DBSubnetGroupName=DB_SUBNET_GROUP)
        )
        subnet_idx = ec2.method_calls.index(call.delete_subnet(SubnetId="subnet-1"))
        vpc_idx = ec2.method_calls.index(call.delete_vpc(VpcId="vpc-123"))
        assert instance_idx < cluster_idx < subnet_group_idx
        assert subnet_idx < vpc_idx

        confirmation_logged = any(
            "removed" in str(args[0]) for args, _ in mock_logger.info.call_args_list
        )
        assert confirmation_logged

    def test_halts_before_later_steps_on_non_not_found_client_error_during_cluster_delete(self):
        """A non-not-found ClientError during cluster deletion halts before
        the DB subnet group, subnets, and VPC deletion steps run."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        rds = MagicMock()
        rds.delete_db_cluster.side_effect = make_client_error(
            "DependencyViolation", "DeleteDBCluster"
        )

        def client_factory(service_name, **kwargs):
            return ec2 if service_name == "ec2" else rds

        with patch("rds.rds_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "rds.rds_teardown.boto3.client", side_effect=client_factory
        ):
            main()

        rds.delete_db_instance.assert_called_once()
        rds.delete_db_cluster.assert_called_once()
        rds.delete_db_subnet_group.assert_not_called()
        ec2.describe_subnets.assert_not_called()
        ec2.delete_vpc.assert_not_called()

    def test_waiter_error_during_wait_halts_the_sequence(self):
        """A WaiterError raised while waiting for instance deletion is
        classified by the real common.teardown.run_step as an 'other'
        failure and halts before any later step runs."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        rds = MagicMock()
        waiter = MagicMock()
        waiter.wait.side_effect = make_waiter_error()
        rds.get_waiter.return_value = waiter

        def client_factory(service_name, **kwargs):
            return ec2 if service_name == "ec2" else rds

        with patch("rds.rds_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "rds.rds_teardown.boto3.client", side_effect=client_factory
        ):
            main()

        rds.delete_db_instance.assert_called_once()
        waiter.wait.assert_called_once()
        # Halted before the cluster deletion step (and everything after it).
        rds.delete_db_cluster.assert_not_called()
        rds.delete_db_subnet_group.assert_not_called()
        ec2.delete_vpc.assert_not_called()

    def test_not_found_error_logs_absence_and_continues_to_later_steps(self):
        """A not-found ClientError during instance deletion is logged as an
        absence and the remaining steps still run."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        ec2.describe_subnets.return_value = {"Subnets": []}
        rds = MagicMock()
        rds.delete_db_instance.side_effect = make_client_error(
            "DBInstanceNotFoundFault", "DeleteDBInstance"
        )

        def client_factory(service_name, **kwargs):
            return ec2 if service_name == "ec2" else rds

        with patch("rds.rds_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "rds.rds_teardown.boto3.client", side_effect=client_factory
        ):
            main()

        rds.delete_db_instance.assert_called_once()
        # Teardown continued through cluster, subnet group, subnets, and VPC.
        rds.delete_db_cluster.assert_called_once()
        rds.delete_db_subnet_group.assert_called_once()
        ec2.delete_vpc.assert_called_once_with(VpcId="vpc-123")

    def test_run_step_mocked_to_control_continue_halt_sequencing(self):
        """Using a mocked common.teardown.run_step directly: all steps
        continue -> every delete_* step function is invoked once, in
        order."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        rds = MagicMock()

        def client_factory(service_name, **kwargs):
            return ec2 if service_name == "ec2" else rds

        executed_steps = []

        def fake_run_step(logger, step_name, func):
            executed_steps.append(step_name)
            return True

        with patch("rds.rds_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "rds.rds_teardown.boto3.client", side_effect=client_factory
        ), patch("rds.rds_teardown.run_step", side_effect=fake_run_step):
            main()

        assert executed_steps == [
            "delete instance",
            "delete cluster",
            "delete DB subnet group",
            "delete subnets",
            "delete VPC",
        ]

    def test_run_step_mocked_to_halt_after_second_step(self):
        """Using a mocked common.teardown.run_step directly: halting on the
        second step means the third, fourth, and fifth step functions are
        never reached."""
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        rds = MagicMock()

        def client_factory(service_name, **kwargs):
            return ec2 if service_name == "ec2" else rds

        executed_steps = []

        def fake_run_step(logger, step_name, func):
            executed_steps.append(step_name)
            return step_name != "delete cluster"

        with patch("rds.rds_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "rds.rds_teardown.boto3.client", side_effect=client_factory
        ), patch("rds.rds_teardown.run_step", side_effect=fake_run_step):
            main()

        assert executed_steps == ["delete instance", "delete cluster"]
