"""R2 (S3-compatible) helpers.

Keys look like 2026/12/29/v2/01.png — date prefix, version folder, then the
images numbered in send order. The version is worked out by asking R2 which
version folders already exist under that date, so nothing needs to be tracked
in a database.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


def next_version(r2: S3Client, bucket: str, prefix: str) -> str:
    """Return the next unused version folder name under a date prefix.

    Delimiter='/' makes R2 collapse everything below each version folder into a
    single CommonPrefixes entry, so this is one cheap call regardless of how
    many images are stored. An untouched date returns nothing and yields 'v1' --
    object storage has no directories, so there is nothing to create first.
    """
    resp = r2.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    versions = []
    for cp in resp.get("CommonPrefixes", []):
        seg = cp["Prefix"][len(prefix):].strip("/")
        if seg.startswith("v") and seg[1:].isdigit():
            versions.append(int(seg[1:]))
    return f"v{max(versions) + 1 if versions else 1}"


def put_image(
    r2: S3Client,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
) -> None:
    """Upload one image. Overwrites silently if the key already exists."""
    r2.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)