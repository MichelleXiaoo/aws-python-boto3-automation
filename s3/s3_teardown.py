"""
S3 teardown: deletes the S3 provisioner's object and then its bucket.

Deletes, in order, the object the provisioner uploaded and then the
bucket it created -- the reverse of the order s3_provisioner.py creates
them in, matching the delete-child-then-parent ordering the VPC and RDS
teardown scripts use. S3 requires a bucket be empty before it can be
deleted, so the object must go first.

This is the refactored counterpart of an earlier exploratory script that
combined provisioning and full deletion into a single, always-delete run.
Splitting the two apart (this script and s3_provisioner.py) mirrors the
provision/teardown pairing already used by the VPC and RDS domains, and
resolves the previous limitation where every provisioning run ended by
deleting the object it had just verified -- the object (and now the
bucket) can be inspected between the two runs instead.

Compared to the original combined script, this one additionally:

- Wraps each delete step in :func:`common.teardown.run_step`, which
  classifies a ``ClientError`` as either "resource already absent" (log
  and continue) or "some other failure" (log and halt), matching the VPC
  and RDS teardown scripts' error-handling convention.
- Resolves the target region via :func:`common.config.resolve_region`
  instead of a hardcoded ``REGION`` constant, matching the S3 provisioner.
- Logs through :func:`common.logging_config.get_logger` instead of ``print()``.
"""

from __future__ import annotations

import boto3

from common.config import resolve_region
from common.logging_config import get_logger
from common.teardown import run_step
from s3.s3_provisioner import BUCKET_NAME, OBJECT_KEY, bucket_exists

logger = get_logger(__name__)


def delete_object(s3, bucket: str, key: str) -> None:
    """
    Delete the object at ``key`` within ``bucket``.

    Must be called before :func:`delete_bucket`, since S3 requires a
    bucket be empty before it can be deleted.

    Args:
        s3: A boto3 S3 resource.
        bucket: The bucket containing the object.
        key: The object key to delete.
    """
    s3.Object(bucket, key).delete()
    logger.info("Deleted object '%s' from bucket '%s'.", key, bucket)


def delete_bucket(s3, name: str) -> None:
    """
    Delete the bucket itself.

    Must only be called after :func:`delete_object` has completed, since
    the bucket cannot be deleted while any object still exists within it.

    Args:
        s3: A boto3 S3 resource.
        name: The bucket to delete.
    """
    s3.Bucket(name).delete()
    logger.info("Deleted bucket '%s'.", name)


def main() -> None:
    """
    Tear down the S3 domain's bucket and object.

    Resolves the target region and checks whether the bucket exists first
    (via :func:`s3.s3_provisioner.bucket_exists`, so this always targets
    the same bucket the provisioner creates). If it does not exist, logs
    that there is nothing to clean up and returns without running any
    teardown step. Otherwise deletes the object and then the bucket, in
    that order, via :func:`common.teardown.run_step`, stopping at the
    first step that halts. If every step continues, logs a final
    confirmation that the bucket and object have been removed.
    """
    region = resolve_region()
    s3 = boto3.resource("s3", region_name=region)

    if not bucket_exists(s3, BUCKET_NAME):
        logger.info("No bucket named '%s' found. Nothing to clean up.", BUCKET_NAME)
        return

    logger.info("Cleaning up bucket '%s'...", BUCKET_NAME)

    steps = (
        ("delete object", lambda: delete_object(s3, BUCKET_NAME, OBJECT_KEY)),
        ("delete bucket", lambda: delete_bucket(s3, BUCKET_NAME)),
    )

    for step_name, step_func in steps:
        if not run_step(logger, step_name, step_func):
            return

    logger.info("Cleanup complete: bucket '%s' and its object were removed.", BUCKET_NAME)


if __name__ == "__main__":
    main()
