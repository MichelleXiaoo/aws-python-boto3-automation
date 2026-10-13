"""
RDS teardown: removes the RDS provisioner's resources in dependency order.

Deletes, in strict sequence, the DB instance (waiting for its deletion to
complete), the DB cluster (waiting for its deletion to complete), the DB
subnet group, the subnets, and finally the VPC itself -- the reverse of
the order the RDS provisioner creates them in, so each delete call never
runs into a dependency failure from a resource that still depends on it.

This is the refactored counterpart of an earlier exploratory script that
was already correct in its use of waiters -- unlike the other legacy
scripts, it had no bug to fix here. Compared to that version, this one
additionally:

- Wraps every step in :func:`common.teardown.run_step`, which classifies
  each ``ClientError`` (including a waiter's ``WaiterError``) as either
  "resource already absent" (log and continue) or "some other failure"
  (log and halt), instead of two separate, hand-rolled ``try/except``
  blocks around the instance/cluster deletion only.
- Resolves the target region via :func:`common.config.resolve_region`
  instead of a hardcoded ``REGION`` constant, matching the other domain
  scripts.
- Imports ``VPC_NAME``, ``DB_SUBNET_GROUP``, ``DB_CLUSTER_ID``, and
  ``INSTANCE_ID`` from :mod:`rds.rds_provisioner` instead of redefining
  them, so this script always targets the exact same resources the RDS
  provisioner creates (including RDS's own, distinct VPC -- see that
  module's docstring), matching how ``vpc/vpc_teardown.py`` imports
  ``VPC_NAME``/``IGW_NAME`` from ``vpc_provisioner``.
- Logs through :func:`common.logging_config.get_logger` instead of ``print()``.
"""

from __future__ import annotations

import boto3

from common.config import resolve_region
from common.logging_config import get_logger
from common.teardown import run_step
from rds.rds_provisioner import DB_CLUSTER_ID, DB_SUBNET_GROUP, INSTANCE_ID, VPC_NAME

logger = get_logger(__name__)


def delete_instance(rds, instance_id: str) -> None:
    """
    Delete the DB instance and wait for its deletion to complete.

    The instance must be fully deleted before the cluster can be deleted --
    while any instance still belongs to it, the cluster still has a member.

    Args:
        rds: A boto3 RDS client.
        instance_id: The identifier of the DB instance to delete.
    """
    rds.delete_db_instance(DBInstanceIdentifier=instance_id, SkipFinalSnapshot=True)
    logger.info("Deletion of instance '%s' requested. Waiting...", instance_id)
    rds.get_waiter("db_instance_deleted").wait(DBInstanceIdentifier=instance_id)
    logger.info("Instance '%s' was deleted.", instance_id)


def delete_cluster(rds, cluster_id: str) -> None:
    """
    Delete the DB cluster and wait for its deletion to complete.

    Must only be called after :func:`delete_instance` has completed, since
    a cluster cannot be deleted while it still has a member instance. The
    cluster must be fully deleted before the DB subnet group can be
    deleted.

    Args:
        rds: A boto3 RDS client.
        cluster_id: The identifier of the DB cluster to delete.
    """
    rds.delete_db_cluster(DBClusterIdentifier=cluster_id, SkipFinalSnapshot=True)
    logger.info("Deletion of cluster '%s' requested. Waiting...", cluster_id)
    rds.get_waiter("db_cluster_deleted").wait(DBClusterIdentifier=cluster_id)
    logger.info("Cluster '%s' was deleted.", cluster_id)


def delete_db_subnet_group(rds, name: str) -> None:
    """
    Delete the DB subnet group.

    Must only be called after :func:`delete_cluster` has completed, since
    a DB subnet group cannot be deleted while a cluster still references it.

    Args:
        rds: A boto3 RDS client.
        name: The DB subnet group name to delete.
    """
    rds.delete_db_subnet_group(DBSubnetGroupName=name)
    logger.info("Deleted DB subnet group '%s'.", name)


def delete_subnets(ec2, vpc_id: str) -> None:
    """
    Delete every subnet belonging to ``vpc_id``.

    Must only be called after :func:`delete_db_subnet_group` has
    completed, since a subnet referenced by a DB subnet group cannot
    otherwise be safely removed. Mirrors ``vpc.vpc_teardown.delete_subnets``
    exactly in behavior.

    Args:
        ec2: A boto3 EC2 client.
        vpc_id: The VPC whose subnets should be deleted.
    """
    response = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    for subnet in response["Subnets"]:
        subnet_id = subnet["SubnetId"]
        ec2.delete_subnet(SubnetId=subnet_id)
        logger.info("Deleted subnet %s (%s).", subnet_id, subnet.get("CidrBlock"))


def delete_vpc(ec2, vpc_id: str) -> None:
    """
    Delete the VPC itself.

    Must only be called after :func:`delete_subnets` has completed, since
    the VPC cannot be deleted while any subnet still exists within it.

    Args:
        ec2: A boto3 EC2 client.
        vpc_id: The VPC to delete.
    """
    ec2.delete_vpc(VpcId=vpc_id)
    logger.info("Deleted VPC %s.", vpc_id)


def main() -> None:
    """
    Tear down the RDS bonus domain's resources in dependency order.

    Resolves the target region and looks up RDS's own VPC by its ``Name``
    tag first (imported from :mod:`rds.rds_provisioner`, so this always
    targets the same VPC the RDS provisioner creates -- not the VPC
    domain's).
    If no matching VPC exists, logs that there is nothing to clean up and
    returns without running any teardown step. Otherwise runs
    :func:`delete_instance`, :func:`delete_cluster`,
    :func:`delete_db_subnet_group`, :func:`delete_subnets`, and
    :func:`delete_vpc` in order via :func:`common.teardown.run_step`,
    stopping at the first step that halts. A waiter timing out
    (``WaiterError``) during either wait is classified by ``run_step`` as
    an "other" failure and halts the sequence, same as any other
    non-not-found ``ClientError``. If every step continues, logs a final
    confirmation that the RDS domain's resources have been removed.
    """
    region = resolve_region()
    ec2 = boto3.client("ec2", region_name=region)
    rds = boto3.client("rds", region_name=region)

    response = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [VPC_NAME]}])
    matches = response["Vpcs"]
    if not matches:
        logger.info("No VPC named '%s' found. Nothing to clean up.", VPC_NAME)
        return

    vpc_id = matches[0]["VpcId"]
    logger.info("Cleaning up RDS domain VPC '%s' (%s)...", VPC_NAME, vpc_id)

    steps = (
        ("delete instance", lambda: delete_instance(rds, INSTANCE_ID)),
        ("delete cluster", lambda: delete_cluster(rds, DB_CLUSTER_ID)),
        ("delete DB subnet group", lambda: delete_db_subnet_group(rds, DB_SUBNET_GROUP)),
        ("delete subnets", lambda: delete_subnets(ec2, vpc_id)),
        ("delete VPC", lambda: delete_vpc(ec2, vpc_id)),
    )

    for step_name, step_func in steps:
        if not run_step(logger, step_name, step_func):
            return

    logger.info(
        "Cleanup complete: RDS domain VPC '%s' and its associated resources were removed.",
        VPC_NAME,
    )


if __name__ == "__main__":
    main()
