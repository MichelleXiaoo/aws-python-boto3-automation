"""Unit tests for s3/s3_provisioner.py.

Covers bucket_exists (true/false), ensure_bucket (creates when absent,
including the us-east-1 special case, and skips when present),
upload_object/read_object individually, and main()'s full
create -> upload -> read -> update -> read sequence (leaving the bucket
and object in place), including a ClientError injected at a step halting
before later steps run. These are the SMOKE/EXAMPLE scenarios; the full
Hypothesis property-test suite is intentionally out of scope here.

The boto3 S3 resource is mocked throughout via unittest.mock; no real AWS
calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from s3.s3_provisioner import (
    BUCKET_NAME,
    INITIAL_CONTENT,
    OBJECT_KEY,
    UPDATED_CONTENT,
    bucket_exists,
    ensure_bucket,
    main,
    read_object,
    upload_object,
)


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


class TestBucketExists:
    def test_true_when_name_present(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket("other-bucket"), make_bucket("my-bucket")]

        assert bucket_exists(s3, "my-bucket") is True

    def test_false_when_name_absent(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket("other-bucket")]

        assert bucket_exists(s3, "my-bucket") is False

    def test_false_when_no_buckets(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = []

        assert bucket_exists(s3, "my-bucket") is False


class TestEnsureBucket:
    def test_skips_create_when_bucket_exists(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket("my-bucket")]

        ensure_bucket(s3, "my-bucket", "ap-southeast-2")

        s3.create_bucket.assert_not_called()

    def test_creates_with_location_constraint_outside_us_east_1(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = []

        ensure_bucket(s3, "my-bucket", "ap-southeast-2")

        s3.create_bucket.assert_called_once_with(
            Bucket="my-bucket",
            CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
        )

    def test_creates_without_location_constraint_in_us_east_1(self):
        """us-east-1 is AWS's special case: create_bucket rejects an
        explicit LocationConstraint for that region."""
        s3 = MagicMock()
        s3.buckets.all.return_value = []

        ensure_bucket(s3, "my-bucket", "us-east-1")

        s3.create_bucket.assert_called_once_with(Bucket="my-bucket")


class TestUploadObject:
    def test_puts_body_on_object(self):
        s3 = MagicMock()
        obj = MagicMock()
        s3.Object.return_value = obj

        upload_object(s3, "my-bucket", "my-key", b"content")

        s3.Object.assert_called_once_with("my-bucket", "my-key")
        obj.put.assert_called_once_with(Body=b"content")


class TestReadObject:
    def test_returns_body_bytes(self):
        s3 = MagicMock()
        obj = MagicMock()
        obj.get.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"content"))}
        s3.Object.return_value = obj

        result = read_object(s3, "my-bucket", "my-key")

        assert result == b"content"
        s3.Object.assert_called_once_with("my-bucket", "my-key")

    def test_propagates_client_error(self):
        s3 = MagicMock()
        obj = MagicMock()
        obj.get.side_effect = make_client_error("NoSuchKey", "GetObject")
        s3.Object.return_value = obj

        try:
            read_object(s3, "my-bucket", "my-key")
            assert False, "expected ClientError to propagate"
        except ClientError as err:
            assert err.response["Error"]["Code"] == "NoSuchKey"


class TestMain:
    def _configure_object_mock(self, s3, read_sequence):
        """Configure s3.Object(...) so each call to .get()["Body"].read()
        returns the next value in read_sequence (an exception instance is
        raised instead of returned)."""
        obj = MagicMock()
        get_results = []
        for item in read_sequence:
            if isinstance(item, Exception):
                get_results.append(item)
            else:
                body = MagicMock(read=MagicMock(return_value=item))
                get_results.append({"Body": body})
        obj.get.side_effect = get_results
        s3.Object.return_value = obj
        return obj

    def test_full_provisioning_sequence_and_call_order(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = []
        s3.meta.client.meta.region_name = "ap-southeast-2"
        obj = self._configure_object_mock(s3, [INITIAL_CONTENT, UPDATED_CONTENT])

        with patch("s3.s3_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_provisioner.boto3.resource", return_value=s3
        ) as mock_resource:
            main()

        mock_resource.assert_called_once_with("s3", region_name="ap-southeast-2")

        # Bucket created (zero matches).
        s3.create_bucket.assert_called_once_with(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
        )

        # upload (initial) -> read -> upload (update) -> read; nothing deleted.
        assert obj.put.call_count == 2
        put_calls = [c.kwargs["Body"] for c in obj.put.call_args_list]
        assert put_calls == [INITIAL_CONTENT, UPDATED_CONTENT]

        assert obj.get.call_count == 2
        obj.delete.assert_not_called()

        call_names = [c[0] for c in obj.method_calls]
        assert call_names == ["put", "get", "put", "get"]

    def test_reuses_existing_bucket_without_creating(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket(BUCKET_NAME)]
        s3.meta.client.meta.region_name = "ap-southeast-2"
        self._configure_object_mock(s3, [INITIAL_CONTENT, UPDATED_CONTENT])

        with patch("s3.s3_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_provisioner.boto3.resource", return_value=s3
        ):
            main()

        s3.create_bucket.assert_not_called()

    def test_resolves_region_before_constructing_resource(self):
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket(BUCKET_NAME)]
        s3.meta.client.meta.region_name = "ap-southeast-2"
        self._configure_object_mock(s3, [INITIAL_CONTENT, UPDATED_CONTENT])

        call_order = []
        with patch(
            "s3.s3_provisioner.resolve_region",
            side_effect=lambda: call_order.append("resolve_region") or "ap-southeast-2",
        ), patch(
            "s3.s3_provisioner.boto3.resource",
            side_effect=lambda *a, **kw: call_order.append("boto3.resource") or s3,
        ):
            main()

        assert call_order == ["resolve_region", "boto3.resource"]

    def test_halts_on_client_error_during_upload_before_read(self):
        """A ClientError during the initial upload halts before any read
        or later step runs."""
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket(BUCKET_NAME)]
        s3.meta.client.meta.region_name = "ap-southeast-2"
        obj = MagicMock()
        obj.put.side_effect = make_client_error("InternalError", "PutObject")
        s3.Object.return_value = obj

        with patch("s3.s3_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_provisioner.boto3.resource", return_value=s3
        ):
            main()  # must not raise

        obj.put.assert_called_once()
        obj.get.assert_not_called()

    def test_halts_on_client_error_during_ensure_bucket_before_upload(self):
        """A ClientError during bucket creation halts before upload."""
        s3 = MagicMock()
        s3.buckets.all.return_value = []
        s3.create_bucket.side_effect = make_client_error("InternalError", "CreateBucket")
        s3.meta.client.meta.region_name = "ap-southeast-2"
        obj = MagicMock()
        s3.Object.return_value = obj

        with patch("s3.s3_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_provisioner.boto3.resource", return_value=s3
        ):
            main()  # must not raise

        obj.put.assert_not_called()

    def test_halts_on_mismatched_content_after_upload(self):
        """If the read-back content doesn't match what was uploaded,
        main() logs an error and stops before the update step."""
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket(BUCKET_NAME)]
        s3.meta.client.meta.region_name = "ap-southeast-2"
        obj = self._configure_object_mock(s3, [b"not-the-right-content"])

        with patch("s3.s3_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_provisioner.boto3.resource", return_value=s3
        ):
            main()  # must not raise

        # Only the initial upload happened; update (2nd put) never ran.
        assert obj.put.call_count == 1

    def test_halts_on_mismatched_content_after_update(self):
        """If the read-back content after the update doesn't match,
        main() logs an error without raising."""
        s3 = MagicMock()
        s3.buckets.all.return_value = [make_bucket(BUCKET_NAME)]
        s3.meta.client.meta.region_name = "ap-southeast-2"
        obj = self._configure_object_mock(
            s3, [INITIAL_CONTENT, b"not-the-updated-content"]
        )

        with patch("s3.s3_provisioner.resolve_region", return_value="ap-southeast-2"), patch(
            "s3.s3_provisioner.boto3.resource", return_value=s3
        ):
            main()  # must not raise

        assert obj.put.call_count == 2
        assert obj.get.call_count == 2
