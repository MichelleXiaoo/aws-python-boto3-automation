"""
EC2 teardown: terminates the EC2 provisioner's instance.

Looks up the instance tagged with the configured instance name (the same
tag ec2_provisioner.py uses) and terminates it.

This is the refactored counterpart of an earlier exploratory script that
combined acquisition and termination into a single, always-terminate run.
Splitting the two apart (this script and ec2_provisioner.py) mirrors the
provision/teardown pairing already used by the VPC and RDS domains, and
lets the acquired instance be inspected between the two runs instead of
being torn down at the end of every single invocation.

Compared to the original combined script, this one additionally:

- Wraps termination in :func:`common.teardown.run_step`, which classifies
  a ``ClientError`` as either "resource already absent" (log and
  continue) or "some other failure" (log and halt), matching the VPC and
  RDS teardown scripts' error-handling convention.
- Resolves the target region via :func:`common.config.resolve_region`
  instead of a hardcoded ``REGION`` constant, matching the EC2 provisioner.
- Logs through :func:`common.logging_config.get_logger` instead of ``print()``.
"""

from __future__ import annotations

import boto3

from common.config import resolve_region
from common.logging_config import get_logger
from common.teardown import run_step
from ec2.ec2_provisioner import INSTANCE_NAME, find_existing_instance

logger = get_logger(__name__)


def terminate_instance(ec2, instance_id: str) -> None:
    """
    Terminate the instance identified by ``instance_id``.

    Args:
        ec2: A boto3 EC2 client.
        instance_id: The id of the instance to terminate.

    Raises:
        ClientError: If the ``terminate_instances`` call fails. Left
            uncaught here so :func:`common.teardown.run_step` (in
            :func:`main`) can classify and log it.
    """
    ec2.terminate_instances(InstanceIds=[instance_id])
    logger.info("Terminated instance %s.", instance_id)


def main() -> None:
    """
    Tear down the EC2 domain's instance.

    Resolves the target region and looks up the instance by its ``Name``
    tag first (via :func:`ec2.ec2_provisioner.find_existing_instance`, so
    this always targets the same instance the provisioner acquires). If no
    matching instance exists, logs that there is nothing to clean up and
    returns without attempting termination. Otherwise terminates it via
    :func:`common.teardown.run_step`, which logs a final confirmation on
    success or halts and logs the error on any other failure.
    """
    region = resolve_region()
    ec2 = boto3.client("ec2", region_name=region)

    instance_id = find_existing_instance(ec2, INSTANCE_NAME)
    if instance_id is None:
        logger.info("No instance named '%s' found. Nothing to clean up.", INSTANCE_NAME)
        return

    logger.info("Cleaning up instance '%s' (%s)...", INSTANCE_NAME, instance_id)

    if not run_step(logger, "terminate instance", lambda: terminate_instance(ec2, instance_id)):
        return

    logger.info("Cleanup complete: instance '%s' was terminated.", INSTANCE_NAME)


if __name__ == "__main__":
    main()
