from dotenv import load_dotenv
from dataclasses import dataclass, field
import boto3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
import os
from pathlib import Path

# Local development may use .env; deployments can inject environment variables.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

#R2 calls
@dataclass(frozen=True)
class R2Config:
    endpoint_url: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    bucket_name: str

    @classmethod
    def from_env(cls) -> "R2Config":
        return cls(
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket_name=os.environ["R2_BUCKET_NAME"],
        )

def _int_set(name: str) -> frozenset[int]:
    raw = os.environ[name]
    try:
        return frozenset(int(p) for p in raw.replace(",", " ").split())
    except ValueError as e:
        raise RuntimeError(f"{name} must be comma-separated integers, got {raw!r}") from e

@dataclass(frozen=True)
class AppConfig:
    bot_token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    r2: R2Config

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            bot_token=os.environ["TELEGRAM_BOT_API_TOKEN"],
            allowed_user_ids=_int_set("ALLOWED_USER_IDS"),
            r2=R2Config.from_env(),
        )

config = AppConfig.from_env()

r2: S3Client = boto3.client(
    service_name="s3",
    endpoint_url=config.r2.endpoint_url,
    aws_access_key_id=config.r2.access_key_id,
    aws_secret_access_key=config.r2.secret_access_key,
    region_name="auto",
)
bucket = config.r2.bucket_name