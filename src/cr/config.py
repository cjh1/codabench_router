from typing import Dict, Optional
from enum import Enum

from pydantic import AnyUrl, BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class CodaBenchQueue(BaseModel):
    name: str
    rabbitmq_queue: Optional[str] = "compute-worker"
    rabbitmq_exchange: Optional[str] = "compute-worker"
    rabbitmq_routing_key: Optional[str] = "compute-worker"
    rabbitmq_broker_url: AnyUrl


class CodaBenchUserRouterKey(str, Enum):
    username = "username"
    email = "email"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    SOURCE_QUEUE_NAME: str
    DEFAULT_TARGET_QUEUE_NAME: str
    ROUTES: Dict[str, str]
    QUEUES: Dict[str, CodaBenchQueue]
    CODABENCH_URL: AnyUrl
    CODABENCH_API_TOKEN: str
    CODABENCH_COMPETITION: int
    CODABENCH_USER_ROUTING_KEY: CodaBenchUserRouterKey


settings = Settings()
