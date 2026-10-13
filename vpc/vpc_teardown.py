"""
VPC teardown: removes the VPC provisioner's resources in dependency order.

Deletes, in strict sequence, the subnets, the non-main route tables, the
internet gateway, and finally the VPC itself -- the reverse of the order
the VPC provisioner creates them in, so each delete call never runs into a
``DependencyViolation`` from a resource that still depends on it.

This is the refactored counterpart of an earlier exploratory script that
wrapped only the overall teardown sequence in a single broad
``try/except ClientError``. Compared to that version, this one additionally:

- Wraps every step in :func:`common.teardown.run_step`, which classifies
  each ``ClientError`` as either "resource already absent" (log and
  continue) or "some other failure" (log and halt), instead of a single
  broad ``try/except ClientError`` around the whole sequence.
- Resolves the target region via :func:`common.config.resolve_region`
  instead of a hardcoded ``REGION`` constant, matching the VPC provisioner.
- Logs through :func:`common.logging_config.get_logger` instead of ``print()``.
"""

from __future__ import annotations

import boto3

from common.config import resolve_region
from common.logging_config import get_logger
from common.teardown import run_step
from vpc.vpc_provisioner import IGW_NAME, VPC_NAME

logger = get_logger(__name__)


def delete_subnets(ec2, vpc_id: str) -> None:
    """
    Delete every subnet belonging to ``vpc_id``.

    Subnets must be deleted before any non-main route table, since a
    subnet's route table association is removed along with the subnet.

    Args:
        ec2: A boto3 EC2 client.
        vpc_id: The VPC whose subnets should be deleted.
    """
    response = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    for subnet in response["Subnets"]:
        subnet_id = subnet["SubnetId"]
        ec2.delete_subnet(SubnetId=subnet_id)
        logger.info("Deleted subnet %s (%s).", subnet_id, subnet.get("CidrBlock"))


def delete_route_tables(ec2, vpc_id: str) -> None:
    """
    Delete every non-main route table belonging to ``vpc_id``.

    Describes route tables filtered by ``vpc-id`` and skips any route table
    whose associations include a main association -- the main route table
    is deleted automatically when the VPC itself is deleted, matching the
    original script's behavior and comment. Any remaining (non-main)
    associations on a route table are disassociated before the route table
    is deleted.

    Args:
        ec2: A boto3 EC2 client.
        vpc_id: The VPC whose non-main route tables should be deleted.
    """
    response = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    for route_table in response["RouteTables"]:
        associations = route_table.get("Associations", [])
        is_main = any(association.get("Main") for association in associations)
        if is_main:
            # The main route table is removed automatically with the VPC.
            continue

        for association in associations:
            association_id = association.get("RouteTableAssociationId")
            if association_id:
                ec2.disassociate_route_table(AssociationId=association_id)

        route_table_id = route_table["RouteTableId"]
        ec2.delete_route_table(RouteTableId=route_table_id)
        logger.info("Deleted route table %s.", route_table_id)


def delete_igw(ec2, name: str, vpc_id: str) -> None:
    """
    Detach and delete the internet gateway tagged ``name``.

    Describes internet gateways filtered by the ``Name`` tag. For each
    match, every attachment is detached first -- an internet gateway cannot
    be deleted while still attached to a VPC -- and only then is the
    gateway itself deleted.

    Args:
        ec2: A boto3 EC2 client.
        name: The ``Name`` tag value identifying the internet gateway.
        vpc_id: Unused directly (the gateway's own attachments list names
            every VPC it is attached to), but kept for symmetry with the
            provisioner's ``ensure_igw(ec2, name, vpc_id)`` signature.
    """
    response = ec2.describe_internet_gateways(
        Filters=[{"Name": "tag:Name", "Values": [name]}]
    )
    for igw in response["InternetGateways"]:
        igw_id = igw["InternetGatewayId"]
        for attachment in igw.get("Attachments", []):
            attached_vpc_id = attachment["VpcId"]
            ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=attached_vpc_id)
            logger.info("Detached internet gateway %s from VPC %s.", igw_id, attached_vpc_id)
        ec2.delete_internet_gateway(InternetGatewayId=igw_id)
        logger.info("Deleted internet gateway %s.", igw_id)


def delete_vpc(ec2, vpc_id: str) -> None:
    """
    Delete the VPC itself.

    Must only be called after :func:`delete_subnets`, :func:`delete_route_
    tables`, and :func:`delete_igw` have each completed, since the VPC
    cannot be deleted while any of those dependent resources still exist.

    Args:
        ec2: A boto3 EC2 client.
        vpc_id: The VPC to delete.
    """
    ec2.delete_vpc(VpcId=vpc_id)
    logger.info("Deleted VPC %s.", vpc_id)


def main() -> None:
    """
    Tear down the VPC domain's resources in dependency order.

    Resolves the target region and looks up the VPC by its ``Name`` tag
    first. If no matching VPC exists, logs that there is nothing to clean
    up and returns without running any teardown step. Otherwise runs
    :func:`delete_subnets`, :func:`delete_route_tables`, :func:`delete_igw`,
    and :func:`delete_vpc` in order via :func:`common.teardown.run_step`,
    stopping at the first step that halts. If every step continues, logs a
    final confirmation that the VPC and its associated resources have been
    removed.
    """
    region = resolve_region()
    ec2 = boto3.client("ec2", region_name=region)

    response = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [VPC_NAME]}])
    matches = response["Vpcs"]
    if not matches:
        logger.info("No VPC named '%s' found. Nothing to clean up.", VPC_NAME)
        return

    vpc_id = matches[0]["VpcId"]
    logger.info("Cleaning up VPC '%s' (%s)...", VPC_NAME, vpc_id)

    steps = (
        ("delete subnets", lambda: delete_subnets(ec2, vpc_id)),
        ("delete route tables", lambda: delete_route_tables(ec2, vpc_id)),
        ("delete internet gateway", lambda: delete_igw(ec2, IGW_NAME, vpc_id)),
        ("delete VPC", lambda: delete_vpc(ec2, vpc_id)),
    )

    for step_name, step_func in steps:
        if not run_step(logger, step_name, step_func):
            return

    logger.info("Cleanup complete: VPC '%s' and its associated resources were removed.", VPC_NAME)


if __name__ == "__main__":
    main()
