"""
S3 provisioner: idempotent bucket create-or-reuse plus object write/update.

Performs create-or-reuse -> upload -> read-verify -> update -> read-verify
against a single S3 bucket and object, with every step logged and every
AWS call guarded by its own ``ClientError`` handling. The bucket and
object are left in place after this script finishes; the paired teardown
script (s3_teardown.py) deletes the object and then the bucket.

This is the refactored counterpart of an earlier exploratory script that
had no functions, no error handling, a latent relative-path bug on its
"update" step, and unconditionally deleted the object (and emptied the
bucket) at the end of every run. Compared to that version, this one
additionally:

- Wraps every bucket/object operation in its own local ``try/except
  ClientError`` that logs the failure and returns from :func:`main` early,
  instead of a single broad ``try/except`` around the whole ``main()``
  call.
- Uploads and updates object content directly as in-memory bytes via
  ``Object.put(Body=...)`` instead of writing local temp files and calling
  ``upload_file`` (simpler and more testable; nothing in the requirements
  mandates writing to local disk).
- Resolves the target region via :func:`common.config.resolve_region`
  instead of implicitly trusting whatever the default boto3 session
  resolves to, while still deriving the bucket-creation region from the
  active session's client (``s3.meta.client.meta.region_name``) --
  the two values agree, since the ``s3`` resource below is itself
  constructed with the resolved region.
- Logs through :func:`common.logging_config.get_logger` instead of
  ``print()``.
- Resolves credentials and region exclusively through boto3's standard
  credential provider chain: no access key, secret key, or region is
  embedded in source.
- Splits provisioning from deletion into separate scripts (this one and
  s3_teardown.py), so running this script twice in a row converges on the
  object's final content instead of always ending in delete -- making the
  bucket *and* object lifecycle genuinely idempotent, not just the bucket
  check.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from common.config import resolve_region
from common.logging_config import get_logger

logger = get_logger(__name__)

#: The bucket this script creates-or-reuses and performs the lifecycle
#: against. A clearly portfolio-specific name (rather than reusing the
#: original script's account-specific ``mich-dct-crud-1``), since this
#: project is meant to run against any reviewer's own AWS account.
BUCKET_NAME = "boto3-portfolio-s3-demo"

#: The object key the upload/update cycle is performed against. Also used
#: by s3_teardown.py to delete the object.
OBJECT_KEY = "demo-object.txt"

#: Content uploaded on the initial write.
INITIAL_CONTENT = b"Hello S3! This greeting is from the initial upload."

#: Content the object is updated to on the second write.
UPDATED_CONTENT = b"Hello S3! This greeting is from the updated upload."


def bucket_exists(s3, name: str) -> bool:
    """
    Return True if a bucket named ``name`` already exists.

    Lists every bucket visible to the current credentials via
    ``s3.buckets.all()`` and checks for a name match, matching the
    original script's style and the existing codebase's convention of
    comparing against ``[b.name for b in s3.buckets.all()]``.

    Args:
        s3: A boto3 S3 resource.
        name: The bucket name to check for.

    Returns:
        True if a bucket named ``name`` exists, else False.
    """
    return name in (bucket.name for bucket in s3.buckets.all())


def ensure_bucket(s3, name: str, region: str) -> None:
    """
    Create the bucket ``name`` if it does not already exist.

    If :func:`bucket_exists` reports the bucket already exists, this
    reuses it without attempting to create it. Otherwise, creates it
    using ``region`` -- the region resolved from the active boto3
    session. ``us-east-1`` is a special
    case: AWS rejects an explicit ``LocationConstraint`` for that region,
    so ``CreateBucketConfiguration`` is only included for every other
    region, matching the original script's conditional.

    Args:
        s3: A boto3 S3 resource.
        name: The bucket name to ensure exists.
        region: The region to create the bucket in, if it must be created.
    """
    if bucket_exists(s3, name):
        logger.info("Bucket '%s' already exists. Reusing.", name)
        return

    logger.info("Bucket '%s' does not exist. Creating in region '%s'.", name, region)
    if region == "us-east-1":
        # us-east-1 is AWS's one special case: create_bucket rejects an
        # explicit LocationConstraint for this region.
        s3.create_bucket(Bucket=name)
    else:
        s3.create_bucket(
            Bucket=name,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    logger.info("Created bucket '%s'.", name)


def upload_object(s3, bucket: str, key: str, content: bytes) -> None:
    """
    Upload ``content`` to ``key`` within ``bucket``.

    Builds the object body directly from in-memory bytes via
    ``Object.put``, rather than round-tripping through a local temp file
    as the original script did, since nothing in the requirements
    mandates writing local files and this is simpler to test.

    Args:
        s3: A boto3 S3 resource.
        bucket: The bucket to upload to.
        key: The object key to write.
        content: The raw bytes to store at ``key``.
    """
    s3.Object(bucket, key).put(Body=content)


def read_object(s3, bucket: str, key: str) -> bytes:
    """
    Return the raw bytes stored at ``key`` within ``bucket``.

    Args:
        s3: A boto3 S3 resource.
        bucket: The bucket to read from.
        key: The object key to read.

    Returns:
        The object's full body, read into memory as bytes.

    Raises:
        botocore.exceptions.ClientError: If ``key`` does not exist in
            ``bucket`` (a not-found error), or the call otherwise fails.
    """
    return s3.Object(bucket, key).get()["Body"].read()


def main() -> None:
    """
    Provision the bucket and object, left in place for inspection.

    Resolves the target region first, before constructing the ``s3``
    resource, so credentials and region are resolved exclusively through
    boto3's standard credential provider chain. Then runs, in strict
    sequence: create-or-reuse the bucket, upload the initial content, read
    back and verify it, update (re-upload) the object, read back and
    verify the update.

    Each step is wrapped in its own ``try/except ClientError`` that logs
    the failure and returns early, so a failure partway through halts all
    later steps without raising an unhandled exception.

    Unlike the combined script this replaces, the object is *not* deleted
    here -- run s3_teardown.py separately to delete the object and the
    bucket once you're done inspecting them.
    """
    region = resolve_region()
    s3 = boto3.resource("s3", region_name=region)

    # Step 1: create-or-reuse the bucket. The region passed to ensure_bucket
    # is read from the active session's own client ("region resolved from
    # the active boto3 session") -- it agrees with `region` above, since
    # that is what the resource was constructed with.
    try:
        ensure_bucket(s3, BUCKET_NAME, s3.meta.client.meta.region_name)
    except ClientError as err:
        logger.error("Failed to ensure bucket '%s' exists: %s", BUCKET_NAME, err)
        return

    # Step 2: upload the initial object content.
    try:
        upload_object(s3, BUCKET_NAME, OBJECT_KEY, INITIAL_CONTENT)
    except ClientError as err:
        logger.error("Failed to upload object '%s': %s", OBJECT_KEY, err)
        return
    logger.info("Uploaded object '%s' to bucket '%s'.", OBJECT_KEY, BUCKET_NAME)

    # Step 3: read the object back and verify it matches what was uploaded.
    try:
        content = read_object(s3, BUCKET_NAME, OBJECT_KEY)
    except ClientError as err:
        logger.error("Failed to read object '%s' after upload: %s", OBJECT_KEY, err)
        return
    if content != INITIAL_CONTENT:
        logger.error(
            "Content mismatch after upload for '%s': expected %r, got %r.",
            OBJECT_KEY,
            INITIAL_CONTENT,
            content,
        )
        return
    logger.info("Verified object '%s' content matches the uploaded content.", OBJECT_KEY)

    # Step 4: update (re-upload) the object with new content.
    try:
        upload_object(s3, BUCKET_NAME, OBJECT_KEY, UPDATED_CONTENT)
    except ClientError as err:
        logger.error("Failed to update object '%s': %s", OBJECT_KEY, err)
        return
    logger.info("Updated object '%s' with new content.", OBJECT_KEY)

    # Step 5: read the object back again and verify it matches the update.
    try:
        content = read_object(s3, BUCKET_NAME, OBJECT_KEY)
    except ClientError as err:
        logger.error("Failed to read object '%s' after update: %s", OBJECT_KEY, err)
        return
    if content != UPDATED_CONTENT:
        logger.error(
            "Content mismatch after update for '%s': expected %r, got %r.",
            OBJECT_KEY,
            UPDATED_CONTENT,
            content,
        )
        return
    logger.info("Verified object '%s' content matches the updated content.", OBJECT_KEY)
    logger.info(
        "S3 provisioning complete: bucket '%s', object '%s'. "
        "Run 'python -m s3.s3_teardown' to delete them.",
        BUCKET_NAME,
        OBJECT_KEY,
    )


if __name__ == "__main__":
    main()
