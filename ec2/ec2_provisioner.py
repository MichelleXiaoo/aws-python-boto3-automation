"""
EC2 provisioner: idempotent acquire-or-create EC2 instance lifecycle.

Looks up an existing EC2 instance tagged with the configured instance name
(restricted to the non-terminal lifecycle states pending/running/stopping/
stopped), reusing it if found or creating a new one if not. The instance
is left running after this script finishes; the paired teardown script
(ec2_teardown.py) terminates it.

This is the refactored counterpart of an earlier exploratory script that
had no functions, no error handling, hardcoded account-specific values,
and unconditionally terminated the instance it had just created on every
run. Compared to that version, this one additionally:

- Organizes the lookup/create logic into functions, following the same
  describe-then-create structure used by the VPC provisioner.
- Wraps the creation call in a local ``try/except ClientError`` that logs
  the failure and signals it to the caller via a ``None`` return, rather
  than letting an exception propagate uncaught -- this is the "lifecycle
  script" error-handling convention (distinct from the VPC/RDS
  provisioners' convention of raising a custom error that propagates),
  matching the same per-script-category convention used elsewhere in this
  codebase.
- Reads the AMI id, key pair name, and region from configurable constants/
  environment variables via :func:`common.config.get_setting` and
  :func:`common.config.resolve_region`, instead of the original's hardcoded,
  account-specific literals.
- Logs through :func:`common.logging_config.get_logger` instead of ``print()``.
- Splits acquisition from termination into separate scripts (this one and
  ec2_teardown.py), so the instance can be inspected between the two runs
  instead of being torn down at the end of every single invocation.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from common.config import get_setting, resolve_region
from common.logging_config import get_logger

logger = get_logger(__name__)

#: The ``Name`` tag value identifying the instance this script
#: acquires, and the one ec2_teardown.py looks up to terminate.
INSTANCE_NAME = "boto3-test"

#: Instance lifecycle states treated as "already exists" for the purposes
#: of the existing-instance check -- excludes "terminated" and "shutting-down".
ACTIVE_STATES = ("pending", "running", "stopping", "stopped")

#: Environment-variable names (and documented defaults) for the two values
#: the original script hardcoded for a single account. The defaults below
#: are the original author's own example AMI id and key pair name -- they
#: are account/region-specific examples, not guaranteed to be valid in any
#: other account, and are expected to be overridden via the environment
#: variables below for other accounts.
EC2_AMI_ID_ENV_VAR = "EC2_AMI_ID"
DEFAULT_AMI_ID = "ami-0720cb7af233b0529"
EC2_KEY_PAIR_NAME_ENV_VAR = "EC2_KEY_PAIR_NAME"
DEFAULT_KEY_PAIR_NAME = "sydneyAZkey"

#: The instance type used when a new instance must be created, matching
#: the original script's choice of a small, low-cost default.
INSTANCE_TYPE = "t3.micro"


def find_existing_instance(ec2, name: str) -> str | None:
    """
    Return the id of an existing, non-terminal instance tagged ``name``.

    Describes instances filtered to :data:`ACTIVE_STATES` -- excluding
    "terminated" and "shutting-down" -- then matches the ``Name`` tag
    client-side across every reservation and instance in the response,
    since the EC2 API's tag filter alone cannot be combined with the state
    list as directly as iterating client-side (matching the original
    script's approach).

    Also used by ec2_teardown.py to look up the instance to terminate.

    Args:
        ec2: A boto3 EC2 client.
        name: The ``Name`` tag value identifying the instance to look for.

    Returns:
        The first matching instance's id, or ``None`` if no instance in one
        of :data:`ACTIVE_STATES` has a ``Name`` tag equal to ``name``.
    """
    response = ec2.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": list(ACTIVE_STATES)}]
    )

    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            for tag in instance.get("Tags", []):
                if tag["Key"] == "Name" and tag["Value"] == name:
                    return instance["InstanceId"]

    return None


def ensure_instance(ec2, name: str, ami_id: str, key_name: str) -> str | None:
    """
    Return the id of the instance tagged ``name``, creating it if absent.

    Calls :func:`find_existing_instance` first. If a match is found, logs
    the reuse and returns its id. If not, calls ``run_instances`` to launch
    a new instance tagged with ``name`` at creation time via ``TagSpecifications``.

    Unlike the VPC provisioner's ``ensure_*`` functions, a failed creation call
    is not wrapped in a custom error that propagates -- following the
    "lifecycle script" convention, the ``ClientError`` is caught here,
    logged, and signaled to the caller via a ``None`` return so ``main()``
    can halt without raising.

    Args:
        ec2: A boto3 EC2 client.
        name: The ``Name`` tag value identifying this instance -- the
            idempotence key used by the existence check.
        ami_id: The AMI id to launch from, if a new instance must be
            created.
        key_name: The EC2 key pair name to associate with the new
            instance, if one must be created.

    Returns:
        The acquired instance's id, or ``None`` if no existing instance was
        found and the subsequent ``run_instances`` call failed with a
        ``ClientError``.
    """
    existing_id = find_existing_instance(ec2, name)
    if existing_id is not None:
        logger.info("Instance '%s' already exists: %s. Reusing.", name, existing_id)
        return existing_id

    try:
        response = ec2.run_instances(
            ImageId=ami_id,
            MinCount=1,
            MaxCount=1,
            InstanceType=INSTANCE_TYPE,
            KeyName=key_name,
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": name}]}
            ],
        )
    except ClientError as err:
        logger.error("Failed to launch instance '%s': %s", name, err)
        return None

    instance_id = response["Instances"][0]["InstanceId"]
    logger.info("Launched instance '%s': %s.", name, instance_id)
    return instance_id


def main() -> str | None:
    """
    Acquire (reuse or create) an instance and leave it running.

    Resolves the target region first, before constructing any boto3 client.
    Reads the AMI id and key pair name from :func:`common.config.get_setting`,
    falling back to the documented defaults if the corresponding
    environment variables are unset.

    Calls :func:`ensure_instance` to acquire an instance. If acquisition
    failed (a ``ClientError`` was caught and logged inside
    :func:`ensure_instance`), logs that fact and returns ``None``.

    Unlike the combined script this replaces, the acquired instance is
    *not* terminated here -- run ec2_teardown.py separately to tear it
    down once you're done inspecting it.

    Returns:
        The acquired instance's id, or ``None`` if acquisition failed.
    """
    region = resolve_region()
    ami_id = get_setting(EC2_AMI_ID_ENV_VAR, DEFAULT_AMI_ID)
    key_name = get_setting(EC2_KEY_PAIR_NAME_ENV_VAR, DEFAULT_KEY_PAIR_NAME)

    ec2 = boto3.client("ec2", region_name=region)

    instance_id = ensure_instance(ec2, INSTANCE_NAME, ami_id, key_name)
    if instance_id is None:
        logger.error("Could not acquire an instance named '%s'.", INSTANCE_NAME)
        return None

    return instance_id


if __name__ == "__main__":
    acquired_id = main()
    if acquired_id is not None:
        logger.info(
            "EC2 provisioning complete: instance %s is running. "
            "Run 'python -m ec2.ec2_teardown' to terminate it.",
            acquired_id,
        )
