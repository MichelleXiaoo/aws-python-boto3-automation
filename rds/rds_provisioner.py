"""
RDS provisioner: idempotent describe-then-create Aurora Serverless v2
provisioning, as a bonus domain alongside the VPC/S3/EC2 scripts.

Provisions RDS's own VPC and three AZ-spanning subnets, a DB subnet group,
and an Aurora Serverless v2 cluster with one instance, following the same
describe-then-create pattern used by the VPC provisioner. Cluster
readiness is awaited with a boto3 waiter rather than a manual polling
loop, and the database master password is read from a single designated
environment variable rather than a hardcoded value.

This is the refactored counterpart of an earlier exploratory script that
had no functions, used a manual polling loop instead of a waiter, and had
a hardcoded plaintext password. Compared to that version, this one
additionally:

- Organizes every step into a function, instead of unconditional
  module-level statements.
- Replaces the original's ``while True: time.sleep(40)`` polling loop with
  ``rds.get_waiter("db_cluster_available").wait(...)``.
- Reads the master password from the ``RDS_MASTER_PASSWORD`` environment
  variable via :func:`common.config.require_env`, checked *before* any AWS
  call is made, instead of a hardcoded ``PASSWORD`` constant.
- Resolves the target region via :func:`common.config.resolve_region`
  instead of a hardcoded ``REGION`` constant.
- Logs through :func:`common.logging_config.get_logger` instead of
  ``print()``.

Why this module has its own ``ensure_vpc``/``ensure_subnet`` instead of
importing ``vpc.vpc_provisioner``'s: the original scripts accidentally
shared a single VPC tagged ``vpc-boto3`` across the VPC and RDS domains,
so tearing down one domain could break the other. The fix here is for
this script to provision its own VPC and subnets under distinct names
(``vpc-rds-boto3`` rather than ``vpc-boto3``). Importing and calling
``vpc.vpc_provisioner.ensure_vpc``/``ensure_subnet`` directly would
reintroduce a *code* coupling between the two domains (a change to the VPC
module could now break RDS), and it would also make it harder to read one
domain's script end to end without cross-referencing another. Duplicating
the (small) describe-then-create logic locally keeps the RDS domain
self-contained and independently runnable/readable, at the cost of a
small amount of near-duplicate code with ``vpc/vpc_provisioner.py`` -- a
deliberate trade-off.
"""

from __future__ import annotations

from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from common.config import require_env, resolve_region
from common.exceptions import AmbiguousResourceError, ResourceCreationError
from common.logging_config import get_logger

logger = get_logger(__name__)

#: The single designated environment variable this script reads the
#: database master password from. No password is ever hardcoded in this module.

DB_PASSWORD_ENV_VAR = "RDS_MASTER_PASSWORD"

"""
Resource-identifying ``Name`` tag values for RDS's own VPC and
networking resources. Deliberately distinct from the VPC domain's
``vpc-boto3``/``10.0.*`` names (see module docstring's "Why this module
has its own ensure_vpc/ensure_subnet") so the two domains can be
provisioned and torn down independently. The CIDRs are safe to reuse
from the original script's values because they live in a separate VPC
-- there is no overlap within either VPC's own address space.
"""

VPC_NAME = "vpc-rds-boto3"
VPC_CIDR = "10.0.0.0/16"

#: (CIDR, availability-zone suffix) pairs for the three AZ-spanning
#: subnets backing the DB subnet group. Each suffix is appended to the
#: resolved region to derive that subnet's AZ, matching
#: ``vpc/vpc_provisioner.py``'s pattern.

SUBNET_CIDRS = ("10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24")
AZ_SUFFIXES = ("a", "b", "c")

#: Non-secret master username. Only the password is secret and must come
#: from the environment -- the username has no confidentiality
#: requirement, matching the original script.

USERNAME = "rdsadminboto3"

DB_SUBNET_GROUP = "rds-subnet-group-boto3"
DB_CLUSTER_ID = "rds-testing-boto3"
INSTANCE_ID = DB_CLUSTER_ID + "-instance-1"


@dataclass(frozen=True)
class RdsResources:
    """
    The identifiers of every resource this script produces.
    Attributes:
        vpc_id: The id of the provisioned (or reused) VPC backing the RDS domain.
        subnet_ids: The ids of the three provisioned (or reused) subnets
            backing the DB subnet group, in the same order as :data:`SUBNET_CIDRS`.
        db_subnet_group: The name of the provisioned (or reused) DB subnet group.
        cluster_id: The identifier of the provisioned (or reused) DB cluster.
        instance_id: The identifier of the DB instance on that cluster.
    """

    vpc_id: str
    subnet_ids: list[str]
    db_subnet_group: str
    cluster_id: str
    instance_id: str


def ensure_vpc(ec2, name: str, cidr: str) -> str:
    """
    Return the id of the VPC tagged ``name``, creating it if absent.

    Mirrors ``vpc.vpc_provisioner.ensure_vpc`` exactly in behavior (same
    describe-then-create-with-``TagSpecifications`` pattern, same
    ``AmbiguousResourceError``/``ResourceCreationError`` failure modes),
    duplicated locally so this module stays self-contained (see the module
    docstring's "Why this module has its own ensure_vpc/ensure_subnet").

    Args:
        ec2: A boto3 EC2 client.
        name: The ``Name`` tag value identifying this VPC -- the
            idempotence key used by the existence check.
        cidr: The CIDR block to use if the VPC must be created.

    Returns:
        The VPC id, either reused or newly created.

    Raises:
        AmbiguousResourceError: If 2+ VPCs match the ``Name`` tag filter.
            No create or modify call is made in this case.
        ResourceCreationError: If ``create_vpc`` fails with a ``ClientError``.
    """
    resource_type = "vpc"
    response = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [name]}])
    matches = response["Vpcs"]

    if len(matches) > 1:
        matching_ids = [vpc["VpcId"] for vpc in matches]
        logger.error(
            "Ambiguous match for %s '%s': found %d matching resources %s.",
            resource_type,
            name,
            len(matching_ids),
            matching_ids,
        )
        raise AmbiguousResourceError(resource_type, matching_ids)

    if matches:
        vpc_id = matches[0]["VpcId"]
        logger.info("VPC '%s' already exists: %s. Reusing.", name, vpc_id)
        return vpc_id

    try:
        response = ec2.create_vpc(
            CidrBlock=cidr,
            TagSpecifications=[
                {"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": name}]}
            ],
        )
    except ClientError as err:
        logger.error("Failed to create %s '%s': %s", resource_type, name, err)
        raise ResourceCreationError(resource_type, cause=err) from err

    vpc_id = response["Vpc"]["VpcId"]
    logger.info("Created %s '%s': %s.", resource_type, name, vpc_id)
    return vpc_id


def ensure_subnet(ec2, vpc_id: str, cidr: str, az: str) -> str:
    """
    Return the id of the subnet with ``cidr`` in ``vpc_id``, creating it in ``az`` if absent.

    Mirrors ``vpc.vpc_provisioner.ensure_subnet`` exactly in behavior
    (describe filtered by ``vpc-id``/``cidr-block``, same 0/1/2+ branch,
    same ``TagSpecifications``-at-creation and failure-mode conventions),
    duplicated locally for the same domain-independence reason as
    :func:`ensure_vpc`.

    Args:
        ec2: A boto3 EC2 client.
        vpc_id: The VPC the subnet belongs to.
        cidr: The CIDR block identifying this subnet.
        az: The availability zone to create the subnet in, if it must be
            created.

    Returns:
        The subnet id, either reused or newly created.

    Raises:
        AmbiguousResourceError: If 2+ subnets match the filter. No create
            or modify call is made in this case.
        ResourceCreationError: If ``create_subnet`` fails with a ``ClientError``.
    """
    resource_type = "subnet"
    response = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "cidr-block", "Values": [cidr]},
        ]
    )
    matches = response["Subnets"]

    if len(matches) > 1:
        matching_ids = [subnet["SubnetId"] for subnet in matches]
        logger.error(
            "Ambiguous match for %s '%s': found %d matching resources %s.",
            resource_type,
            cidr,
            len(matching_ids),
            matching_ids,
        )
        raise AmbiguousResourceError(resource_type, matching_ids)

    if matches:
        subnet_id = matches[0]["SubnetId"]
        logger.info("Subnet %s already exists: %s. Reusing.", cidr, subnet_id)
        return subnet_id

    tag_name = f"rds-subnet-{az}"
    try:
        response = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=cidr,
            AvailabilityZone=az,
            TagSpecifications=[
                {"ResourceType": "subnet", "Tags": [{"Key": "Name", "Value": tag_name}]}
            ],
        )
    except ClientError as err:
        logger.error("Failed to create %s '%s': %s", resource_type, cidr, err)
        raise ResourceCreationError(resource_type, cause=err) from err

    subnet_id = response["Subnet"]["SubnetId"]
    logger.info("Created %s %s (%s): %s.", resource_type, cidr, az, subnet_id)
    return subnet_id


def ensure_db_subnet_group(rds, name: str, subnet_ids: list[str]) -> None:
    """
    Ensure a DB subnet group named ``name`` exists, spanning ``subnet_ids``.

    Checks existence via ``describe_db_subnet_groups``, catching
    ``rds.exceptions.DBSubnetGroupNotFoundFault`` the way the original
    script does. If the group already exists, nothing is created. If not,
    ``create_db_subnet_group`` is called with ``subnet_ids``.

    Args:
        rds: A boto3 RDS client.
        name: The DB subnet group name -- the idempotence key used by the existence check.
        subnet_ids: The subnet ids the group should span if it must be created.

    Raises:
        ResourceCreationError: If ``create_db_subnet_group`` fails with a ``ClientError``.
    """
    resource_type = "db_subnet_group"
    try:
        rds.describe_db_subnet_groups(DBSubnetGroupName=name)
        logger.info("DB subnet group '%s' already exists. Reusing.", name)
        return
    except rds.exceptions.DBSubnetGroupNotFoundFault:
        pass

    try:
        response = rds.create_db_subnet_group(
            DBSubnetGroupName=name,
            DBSubnetGroupDescription="Subnet group for testing RDS databases with boto3.",
            SubnetIds=subnet_ids,
            Tags=[{"Key": "Environment", "Value": "Testing"}],
        )
    except ClientError as err:
        logger.error("Failed to create %s '%s': %s", resource_type, name, err)
        raise ResourceCreationError(resource_type, cause=err) from err

    arn = response["DBSubnetGroup"]["DBSubnetGroupArn"]
    logger.info("Created %s '%s': %s.", resource_type, name, arn)


def ensure_db_cluster(rds, cluster_id: str, subnet_group: str, password: str) -> None:
    """
    Ensure an Aurora Serverless v2 cluster and instance exist.

    Checks existence via ``describe_db_clusters``, catching
    ``rds.exceptions.DBClusterNotFoundFault``. If the cluster already
    exists, creation and waiting are both skipped -- an existing cluster
    reached by this check is presumed already available, so there is
    nothing new to wait for. If not, creates the cluster
    (``create_db_cluster``) and its instance (``create_db_instance``), then
    waits for the cluster to become available using
    ``rds.get_waiter("db_cluster_available").wait(...)`` instead of a
    manual polling loop.

    Args:
        rds: A boto3 RDS client.
        cluster_id: The DB cluster identifier -- the idempotence key used
            by the existence check.
        subnet_group: The DB subnet group the cluster should use if it
            must be created.
        password: The master user password, read by the caller from
            :data:`DB_PASSWORD_ENV_VAR` -- never a literal in this module.

    Raises:
        ResourceCreationError: If ``create_db_cluster`` or
            ``create_db_instance`` fails with a ``ClientError``.
    """
    resource_type = "db_cluster"
    try:
        rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        logger.info(
            "DB cluster '%s' already exists. Skipping creation and wait.", cluster_id
        )
        return
    except rds.exceptions.DBClusterNotFoundFault:
        pass

    try:
        rds.create_db_cluster(
            Engine="aurora-mysql",
            EngineVersion="8.0.mysql_aurora.3.08.2",
            DBClusterIdentifier=cluster_id,
            MasterUsername=USERNAME,
            MasterUserPassword=password,
            DatabaseName="rds_boto3_testing_db",
            DBSubnetGroupName=subnet_group,
            ServerlessV2ScalingConfiguration={
                "MinCapacity": 0,
                "MaxCapacity": 8,
                "SecondsUntilAutoPause": 300,
            },
        )
        rds.create_db_instance(
            DBInstanceIdentifier=INSTANCE_ID,
            DBClusterIdentifier=cluster_id,
            Engine="aurora-mysql",
            DBInstanceClass="db.serverless",
        )
    except ClientError as err:
        logger.error("Failed to create %s '%s': %s", resource_type, cluster_id, err)
        raise ResourceCreationError(resource_type, cause=err) from err

    logger.info(
        "Created %s '%s' with instance '%s'. Waiting for it to become available.",
        resource_type,
        cluster_id,
        INSTANCE_ID,
    )
    rds.get_waiter("db_cluster_available").wait(DBClusterIdentifier=cluster_id)
    logger.info("DB cluster '%s' is now available.", cluster_id)


def main() -> RdsResources:
    """
    Idempotently provision the RDS bonus domain and return its resource ids.

    Calls :func:`common.config.require_env` for :data:`DB_PASSWORD_ENV_VAR`
    *first*, before resolving the region or constructing any boto3 client.
    If the environment variable is unset or empty, ``require_env`` raises
    ``MissingSecretError`` here, before any AWS call has been made, so no
    AWS resources are created -- the error is left to propagate uncaught,
    consistent with the VPC provisioner's creation-script convention of
    letting errors halt the sequence.

    After the password check succeeds, resolves the target region, then
    provisions, in strict sequence, RDS's own VPC, three AZ-spanning
    subnets, the DB subnet group, and the DB cluster with its instance.

    Returns:
        An :class:`RdsResources` naming every resource provisioned or
        reused during this run.
    """
    password = require_env(DB_PASSWORD_ENV_VAR)

    region = resolve_region()
    ec2 = boto3.client("ec2", region_name=region)
    rds = boto3.client("rds", region_name=region)

    vpc_id = ensure_vpc(ec2, VPC_NAME, VPC_CIDR)

    subnet_ids: list[str] = []
    for cidr, az_suffix in zip(SUBNET_CIDRS, AZ_SUFFIXES):
        az = f"{region}{az_suffix}"
        subnet_ids.append(ensure_subnet(ec2, vpc_id, cidr, az))

    ensure_db_subnet_group(rds, DB_SUBNET_GROUP, subnet_ids)
    ensure_db_cluster(rds, DB_CLUSTER_ID, DB_SUBNET_GROUP, password)

    return RdsResources(
        vpc_id=vpc_id,
        subnet_ids=subnet_ids,
        db_subnet_group=DB_SUBNET_GROUP,
        cluster_id=DB_CLUSTER_ID,
        instance_id=INSTANCE_ID,
    )


if __name__ == "__main__":
    provisioned = main()
    logger.info("RDS provisioning complete: %s", provisioned)
