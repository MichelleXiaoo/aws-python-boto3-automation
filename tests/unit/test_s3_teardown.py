"""Unit tests for s3/s3_teardown.py.

Covers delete_object and delete_bucket individually, main()'s "bucket not
found" early-return path, and main()'s full sequence via the real
common.teardown.run_step -- both the all-steps-succeed happy path (final
confirmation logged, object deleted before bucket) and a halting failure
(a non-not-found ClientError during object deletion, which must stop
before the bucket delete step runs). These are the SMOKE/EXAMPLE
scenarios; the full Hypothesis property-test suite is intentionally out
of scope here.

The boto3 S3 resource is mocked throughout via unittest.mock; no real AWS
calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from s3.s3_teardown import BUCKET_NAME, delete_bucket, delete_object, main


def make_client_error(code: str = "InternalError", operation_name: str = "SomeOp") -> ClientError:
    """Build a real ClientError, as botocore itself would construct one
    from an AWS API error response."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "boom"}},
        operation_name=operation_name,
    )


def make_bucket(name: str) -> MagicMock:
    bucket = MagicMock()
    bucket.name = name
    return bucket


class TestDeleteObject:
    def test_deletes_object(self):
        s3 = MagicMock()
        obj = MagicMock()
        s3.Object.return_value = obj

        delete_object(s3, "my-bucket", "my-key")

        s3.Object.assert_called_once_with("my-bucket", "my-key")
        obj.delete.assert_called_once_with()


class TestDeleteBucket:
    def test_deletes_bucket(self):
        s3 = MagicMock()
        bucket = MagicMock()
        s3.Bucket.return_value = bucket

        delete_bucket(s3, "my-bucket")

        s3.Bucket.assert_called_once_with("my-bucket")
        bucket.delete.assert_called_once_with()


class TestMain:
    def test_returns_early_when_bucket_not_found(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = []

        with patch("s3.s3_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_teardown.boto3.resource", return_value=s3
        ):
            main()

        s3.Object.assert_not_called()
        s3.Bucket.assert_not_called()

    def test_resolves_region_before_constructing_resource(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = []

        call_order = []
        with patch(
            "s3.s3_teardown.resolve_region",
            side_effect=lambda: call_order.append("resolve_region") or "ap-southeast-2",
        ), patch(
            "s3.s3_teardown.boto3.resource",
            side_effect=lambda *a, **kw: call_order.append("boto3.resource") or s3,
        ):
            main()

        assert call_order == ["resolve_region", "boto3.resource"]

    def test_happy_path_deletes_object_then_bucket_and_logs_confirmation(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket(BUCKET_NAME)]
        obj = MagicMock()
        bucket = MagicMock()

        call_order = []
        obj.delete.side_effect = lambda: call_order.append("delete object")
        bucket.delete.side_effect = lambda: call_order.append("delete bucket")
        s3.Object.return_value = obj
        s3.Bucket.return_value = bucket

        with patch("s3.s3_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_teardown.boto3.resource", return_value=s3
        ), patch.object(
            __import__("s3.s3_teardown", fromlist=["logger"]), "logger"
        ) as mock_logger:
            main()

        obj.delete.assert_called_once()
        bucket.delete.assert_called_once()

        # Object deleted before bucket (S3 requires an empty bucket).
        assert call_order == ["delete object", "delete bucket"]

        confirmation_logged = any(
            "removed" in str(args[0]) for args, _ in mock_logger.info.call_args_list
        )
        assert confirmation_logged

    def test_halts_before_bucket_delete_on_non_not_found_client_error(self):
        """A non-not-found ClientError during object deletion halts
        before the bucket delete step runs."""
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket(BUCKET_NAME)]
        obj = MagicMock()
        obj.delete.side_effect = make_client_error("InternalError", "DeleteObject")
        bucket = MagicMock()
        s3.Object.return_value = obj
        s3.Bucket.return_value = bucket

        with patch("s3.s3_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_teardown.boto3.resource", return_value=s3
        ):
            main()  # must not raise

        obj.delete.assert_called_once()
        bucket.delete.assert_not_called()

    def test_not_found_object_logs_absence_and_continues_to_bucket_delete(self):
        """A not-found error deleting the object is logged as an absence
        and the bucket delete step still runs."""
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket(BUCKET_NAME)]
        obj = MagicMock()
        obj.delete.side_effect = make_client_error("NoSuchKey", "DeleteObject")
        bucket = MagicMock()
        s3.Object.return_value = obj
        s3.Bucket.return_value = bucket

        with patch("s3.s3_teardown.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_teardown.boto3.resource", return_value=s3
        ):
            main()  # must not raise

        obj.delete.assert_called_once()
        bucket.delete.assert_called_once()
