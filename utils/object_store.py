"""Small S3-compatible object-store client used with RustFS."""

from __future__ import annotations

import os

import boto3
from botocore.exceptions import ClientError


class ObjectStore:
    def __init__(self) -> None:
        endpoint = os.getenv("OBJECT_STORE_ENDPOINT", "http://rustfs:9000")
        if "://" not in endpoint:
            endpoint = f"http://{endpoint}"
        self.bucket = os.getenv("OBJECT_STORE_BUCKET", "agentic-qe-artifacts")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "rustfsadmin"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "rustfsadmin"),
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def read_bytes(self, object_name: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=object_name)
        return response["Body"].read()

    def read_text(self, object_name: str) -> str:
        return self.read_bytes(object_name).decode("utf-8")

    def write_bytes(self, object_name: str, payload: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=object_name,
            Body=payload,
            ContentType=content_type,
        )

    def count_objects(self, prefix: str = "") -> int:
        """Count objects through the S3 paginator without loading object bodies."""
        paginator = self.client.get_paginator("list_objects_v2")
        return sum(
            int(page.get("KeyCount", len(page.get("Contents", []))))
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
        )
