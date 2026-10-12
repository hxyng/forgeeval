"""boto3 clients configured from settings.

In AWS, credentials come from the ECS task role and nothing here needs configuring. Locally,
``AWS_ENDPOINT_URL`` points every client at the emulators (ElasticMQ for SQS, S3Mock for S3).
"""

from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal, overload

import boto3
from botocore.config import Config

from forgeeval.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_sqs import SQSClient


@overload
def client(service: Literal["sqs"]) -> "SQSClient": ...
@overload
def client(service: Literal["s3"]) -> "S3Client": ...
def client(service: str) -> Any:
    return _client(service)


@lru_cache
def _client(service: str) -> Any:
    s = get_settings()
    endpoint = s.s3_endpoint_url if service == "s3" and s.s3_endpoint_url else s.aws_endpoint_url
    return boto3.client(
        service,  # type: ignore[call-overload]
        region_name=s.aws_region,
        endpoint_url=endpoint,
        config=Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5),
    )
