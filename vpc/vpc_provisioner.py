"""
VPC provisioner: idempotent describe-then-create VPC networking setup.

Provisions a VPC, an internet gateway, a route table, and three subnets
spanning three availability zones, wiring each subnet to the route table via
a route table association. Every resource is looked up first (by its
``Name`` tag, or by ``vpc-id``/``cidr-block`` for subnets) before being
created, so repeated executions against the same account and region
converge on the same set of resources instead of creating duplicates.

This is the refactored counterpart of an earlier exploratory script that
had no functions, used a time.sleep() after creation instead of tagging
at creation time, and had no error handling. Compared to that version,
this one additionally:

- Rejects ambiguous state (2+ resources matching a lookup) by raising
  :class:`~common.exceptions.AmbiguousResourceError` instead of silently
  picking the first match.
- Wraps any ``ClientError`` raised by a ``create_*`` call in
  :class:`~common.exceptions.ResourceCreationError`, which is left to
  propagate out of :func:`main` uncaught -- the ordered sequence of
  ``ensure_*`` calls means that propagation is itself what stops any later
  resource from being created.
- Resolves the target region via :func:`common.config.resolve_region`
  instead of a hardcoded ``REGION`` constant, and derives
  the three subnets' availability zones from that resolved region.
- Logs through :func:`common.logging_config.get_logger` instead of ``print()``.
"""

from __future__ import annotations

from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from common.config import resolve_region
from common.exceptions import AmbiguousResourceError, ResourceCreationError
from common.logging_config import get_logger

logger = get_logger(__name__)

#: Resource-identifying ``Name`` tag values, matching the original script's
#: naming (minor stylistic choice, consistent with the "disposable lab
#: environment" README narrative).
VPC_NAME = "vpc-boto3"
VPC_CIDR = "10.0.0.0/16"
IGW_NAME = "ig-vpc-boto3"
RT_NAME = "rt-vpc-boto3"

#: (CIDR, availability-zone suffix) pairs for the three AZ-spanning subnets.
#: Each suffix is appended to the resolved region to derive that subnet's
#: AZ (e.g. region "ap-southeast-2" + suffix "a" -> "ap-southeast-2a"). This
#: is the natural generalization of the original script's hardcoded
#: `ap-southeast-2a/b/c` list now that the region itself is configurable via
#: `common.config.resolve_region`.
SUBNET_CIDRS = ("10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24")
AZ_SUFFIXES = ("a", "b", "c")


@dataclass(frozen=True)
class VpcResources:
    """
    The identifiers of every resource this script produces.

    Returning these from :func:`main` (rather than only logging them) is
    what makes idempotence testable: calling :func:`main` twice against the
    same account/region state should yield two field-for-field-equal
    instances.

    Attributes:
        vpc_id: The id of the provisioned (or reused) VPC.
        igw_id: The id of the provisioned (or reused) internet gateway.
        route_table_id: The id of the provisioned (or reused) route table.
        subnet_ids: The ids of the three provisioned (or reused) subnets,
            one per configured availability zone, in the same order as
            :data:`SUBNET_CIDRS`.
    """

    vpc_id: str
    igw_id: str
    route_table_id: str
    subnet_ids: list[str]


def ensure_vpc(ec2, name: str, cidr: str) -> str:
    """
    Return the id of the VPC tagged ``name``, creating it if absent.

    Describes VPCs filtered by the ``Name`` tag. Zero matches creates a new
    VPC (tagged at creation time via ``TagSpecifications``); exactly one
    match reuses that VPC's id; two or more matches is treated as an
    ambiguous, unresolvable state.

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
        ResourceCreationError: If ``create_vpc`` fails with a
            ``ClientError``.
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


def ensure_igw(ec2, name: str, vpc_id: str) -> str:
    """
    Return the id of the internet gateway tagged ``name``, creating and
    attaching it to ``vpc_id`` if absent.

    Describes internet gateways filtered by the ``Name`` tag. Zero matches
    creates a new internet gateway (tagged at creation time) and attaches it
    to ``vpc_id``; exactly one match reuses that gateway's id; two or more
    matches is treated as an ambiguous, unresolvable state.

    Args:
        ec2: A boto3 EC2 client.
        name: The ``Name`` tag value identifying this internet gateway.
        vpc_id: The VPC to attach a newly created gateway to.

    Returns:
        The internet gateway id, either reused or newly created.

    Raises:
        AmbiguousResourceError: If 2+ internet gateways match the ``Name``
            tag filter. No create or modify call is made in this case.
        ResourceCreationError: If ``create_internet_gateway`` or the
            subsequent ``attach_internet_gateway`` fails with a
            ``ClientError``.
    """
    resource_type = "internet_gateway"
    response = ec2.describe_internet_gateways(
        Filters=[{"Name": "tag:Name", "Values": [name]}]
    )
    matches = response["InternetGateways"]

    if len(matches) > 1:
        matching_ids = [igw["InternetGatewayId"] for igw in matches]
        logger.error(
            "Ambiguous match for %s '%s': found %d matching resources %s.",
            resource_type,
            name,
            len(matching_ids),
            matching_ids,
        )
        raise AmbiguousResourceError(resource_type, matching_ids)

    if matches:
        igw_id = matches[0]["InternetGatewayId"]
        logger.info("Internet gateway '%s' already exists: %s. Reusing.", name, igw_id)
        return igw_id

    try:
        response = ec2.create_internet_gateway(
            TagSpecifications=[
                {"ResourceType": "internet-gateway", "Tags": [{"Key": "Name", "Value": name}]}
            ]
        )
        igw_id = response["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    except ClientError as err:
        logger.error("Failed to create %s '%s': %s", resource_type, name, err)
        raise ResourceCreationError(resource_type, cause=err) from err

    logger.info(
        "Created %s '%s': %s and attached to VPC %s.", resource_type, name, igw_id, vpc_id
    )
    return igw_id


def ensure_route_table(ec2, name: str, vpc_id: str, igw_id: str) -> str:
    """
    Return the id of the route table tagged ``name`` in ``vpc_id``,
    creating it with a default public route if absent.

    Describes route tables filtered by ``vpc-id`` and the ``Name`` tag.
    Zero matches creates a new route table (tagged at creation time) with a
    ``0.0.0.0/0`` route to ``igw_id``; exactly one match reuses that route
    table's id; two or more matches is treated as an ambiguous,
    unresolvable state.

    Args:
        ec2: A boto3 EC2 client.
        name: The ``Name`` tag value identifying this route table.
        vpc_id: The VPC the route table belongs to.
        igw_id: The internet gateway to route default traffic to when the
            route table must be created.

    Returns:
        The route table id, either reused or newly created.

    Raises:
        AmbiguousResourceError: If 2+ route tables match the filter. No
            create or modify call is made in this case.
        ResourceCreationError: If ``create_route_table`` or the subsequent
            ``create_route`` fails with a ``ClientError``.
    """
    resource_type = "route_table"
    response = ec2.describe_route_tables(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "tag:Name", "Values": [name]},
        ]
    )
    matches = response["RouteTables"]

    if len(matches) > 1:
        matching_ids = [rt["RouteTableId"] for rt in matches]
        logger.error(
            "Ambiguous match for %s '%s': found %d matching resources %s.",
            resource_type,
            name,
            len(matching_ids),
            matching_ids,
        )
        raise AmbiguousResourceError(resource_type, matching_ids)

    if matches:
        rt_id = matches[0]["RouteTableId"]
        logger.info("Route table '%s' already exists: %s. Reusing.", name, rt_id)
        return rt_id

    try:
        response = ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=[
                {"ResourceType": "route-table", "Tags": [{"Key": "Name", "Value": name}]}
            ],
        )
        rt_id = response["RouteTable"]["RouteTableId"]
        ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    except ClientError as err:
        logger.error("Failed to create %s '%s': %s", resource_type, name, err)
        raise ResourceCreationError(resource_type, cause=err) from err

    logger.info(
        "Created %s '%s': %s with public route -> %s.", resource_type, name, rt_id, igw_id
    )
    return rt_id


def ensure_subnet(ec2, vpc_id: str, cidr: str, az: str) -> str:
    """
    Return the id of the subnet with ``cidr`` in ``vpc_id``, creating it
    in ``az`` if absent.

    Describes subnets filtered by ``vpc-id`` and ``cidr-block`` -- not by a
    ``Name`` tag -- since the CIDR block is already unique within a VPC and
    serves as the natural idempotence key for subnets, matching the
    original script's filter style. Zero matches creates a new subnet
    (tagged at creation time); exactly one match reuses that subnet's id;
    two or more matches is treated as an ambiguous, unresolvable state.

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
        ResourceCreationError: If ``create_subnet`` fails with a
            ``ClientError``.
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

    tag_name = f"subnet-{az}"
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


def ensure_association(ec2, rt_id: str, subnet_id: str) -> None:
    """
    Ensure ``subnet_id`` is associated with route table ``rt_id``.

    Describes ``rt_id`` and checks whether ``subnet_id`` already appears
    among its associations. If so, nothing is done. If not, the subnet is
    associated with the route table.

    Unlike the other ``ensure_*`` functions, this check is a boolean
    membership test rather than a describe-filter match count, so there is
    no "2+ matches" ambiguous case here. It is still one of the resources
    checked for existence before creating, consistent with the other
    ``ensure_*`` functions.

    Args:
        ec2: A boto3 EC2 client.
        rt_id: The route table to associate the subnet with.
        subnet_id: The subnet to associate.

    Raises:
        ResourceCreationError: If ``associate_route_table`` fails with a
            ``ClientError``.
    """
    resource_type = "route_table_association"
    response = ec2.describe_route_tables(RouteTableIds=[rt_id])
    route_table = response["RouteTables"][0]
    associated_subnet_ids = [
        association.get("SubnetId") for association in route_table.get("Associations", [])
    ]

    if subnet_id in associated_subnet_ids:
        logger.info(
            "Subnet %s already associated with route table %s. Reusing.", subnet_id, rt_id
        )
        return

    try:
        ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)
    except ClientError as err:
        logger.error(
            "Failed to create %s between subnet %s and route table %s: %s",
            resource_type,
            subnet_id,
            rt_id,
            err,
        )
        raise ResourceCreationError(resource_type, cause=err) from err

    logger.info("Associated subnet %s with route table %s.", subnet_id, rt_id)


def main() -> VpcResources:
    """
    Idempotently provision the VPC domain and return its resource ids.

    Resolves the target region first, before constructing any boto3 client.
    Then provisions, in strict sequence, the VPC, the
    internet gateway, the route table, and each of the three AZ-spanning
    subnets with its route table association. Because the calls run in
    sequence, an uncaught ``AmbiguousResourceError`` or
    ``ResourceCreationError`` raised partway through naturally halts every
    later step while leaving everything created so far in place.

    Returns:
        A :class:`VpcResources` naming every resource provisioned or
        reused during this run.
    """
    region = resolve_region()
    ec2 = boto3.client("ec2", region_name=region)

    vpc_id = ensure_vpc(ec2, VPC_NAME, VPC_CIDR)
    igw_id = ensure_igw(ec2, IGW_NAME, vpc_id)
    route_table_id = ensure_route_table(ec2, RT_NAME, vpc_id, igw_id)

    subnet_ids: list[str] = []
    for cidr, az_suffix in zip(SUBNET_CIDRS, AZ_SUFFIXES):
        az = f"{region}{az_suffix}"
        subnet_id = ensure_subnet(ec2, vpc_id, cidr, az)
        ensure_association(ec2, route_table_id, subnet_id)
        subnet_ids.append(subnet_id)

    return VpcResources(
        vpc_id=vpc_id,
        igw_id=igw_id,
        route_table_id=route_table_id,
        subnet_ids=subnet_ids,
    )


if __name__ == "__main__":
    provisioned = main()
    logger.info("VPC provisioning complete: %s", provisioned)
