"""Raw sample storage. Local directory by default; S3 when S3_BUCKET is set."""
import os
from pathlib import Path


class LocalBlob:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key, data: bytes):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key) -> bytes:
        return (self.root / key).read_bytes()

    def delete(self, key):
        (self.root / key).unlink(missing_ok=True)


class S3Blob:
    def __init__(self, bucket, prefix=""):
        import boto3  # optional dependency
        self.s3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT") or None)
        self.bucket, self.prefix = bucket, prefix.strip("/")

    def _k(self, key):
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key, data: bytes):
        self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data)

    def get(self, key) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=self._k(key))


def make_blob():
    if os.environ.get("S3_BUCKET"):
        return S3Blob(os.environ["S3_BUCKET"], os.environ.get("S3_PREFIX", "perfscope"))
    return LocalBlob(os.environ.get("BLOB_DIR", "./blobs"))
