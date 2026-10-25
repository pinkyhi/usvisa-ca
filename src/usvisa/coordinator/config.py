import os
from dataclasses import dataclass
from pathlib import Path

from usvisa.protocol.messages import GroupConfig
from usvisa.shared.network import server_origin
from usvisa.shared.paths import data_directory, load_environment


@dataclass(frozen=True)
class ServerConfig:
    database: Path
    origin: str
    group: GroupConfig
    amqp_url: str
    management_url: str
    admin_user: str
    admin_password: str
    broker_user: str
    broker_password: str
    insecure_local: bool = False

    @classmethod
    def from_env(cls, database=None):
        load_environment()
        local = os.getenv("ALLOW_INSECURE_LOCALHOST", "false").lower() == "true"
        origin = server_origin(os.environ["PUBLIC_BASE_URL"], allow_local=local)
        user = os.getenv("BROKER_USER", "coordinator")
        password = os.environ["BROKER_PASSWORD"]
        from urllib.parse import quote
        return cls(Path(database or os.getenv("COORDINATOR_DATABASE") or
                        data_directory() / ".coordinator" / "state.sqlite3"), origin,
                   GroupConfig(int(os.getenv("CONSULATE_ID", "95")), os.getenv("CITY", "Vancouver")),
                   os.getenv("BROKER_AMQP_URL") or
                   f"amqp://{quote(user, safe='')}:{quote(password, safe='')}@rabbitmq:5672/visa",
                   os.getenv("BROKER_MANAGEMENT_URL", "http://rabbitmq:15672"),
                   os.environ["BROKER_ADMIN_USER"], os.environ["BROKER_ADMIN_PASSWORD"], user, password, local)
