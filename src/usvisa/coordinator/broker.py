"""RabbitMQ provisioning and AMQP adapter. Only the server has admin credentials."""
import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

import aio_pika
import httpx

from usvisa.protocol.messages import ProtocolError, parse_message


log = logging.getLogger(__name__)


class BrokerAdmin:
    def __init__(self, config):
        self.config = config
        self.client = httpx.AsyncClient(base_url=config.management_url + "/api/",
                                       auth=(config.admin_user, config.admin_password), timeout=15)

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, data=None, *, missing_ok=False):
        response = await self.client.request(method, path, json=data)
        if not (missing_ok and response.status_code == 404):
            response.raise_for_status()
        return response

    async def bootstrap(self):
        c = self.config
        await self.request("PUT", "vhosts/visa", {})
        await self.request("PUT", f"users/{quote(c.broker_user, safe='')}",
                           {"password": c.broker_password, "tags": ""})
        await self.request("PUT", f"permissions/visa/{quote(c.broker_user, safe='')}",
                           {"configure": r"^visa\.(commands|control|slots|coordinator\.inbox)$",
                            "write": r"^visa\.(control|slots|coordinator\.inbox)$",
                            "read": r"^visa\.(commands|coordinator\.inbox)$"})

    async def provision(self, node_id: UUID, secret: str):
        username = f"node_{node_id}"
        queue = f"visa.node.{node_id}.inbox"
        await self.request("PUT", f"users/{username}", {"password": secret, "tags": ""})
        await self.request("PUT", f"queues/visa/{queue}",
                           {"durable": True, "auto_delete": False, "arguments": {"x-queue-type": "classic"}})
        for exchange in ("visa.control", "visa.slots"):
            await self.request("POST", f"bindings/visa/e/{exchange}/q/{queue}",
                               {"routing_key": str(node_id), "arguments": {}})
        # STOMP SEND checks write on the exchange; subscribing to /amq/queue
        # checks read only, so the node can neither configure nor read peers.
        import re
        await self.request("PUT", f"permissions/visa/{username}",
                           {"configure": "^$", "write": r"^visa\.commands$", "read": "^" + re.escape(queue) + "$"})
        await self.request("PUT", f"user-limits/{username}/max-connections", {"value": 1})
        await self.disconnect(username)

    async def disconnect(self, username):
        # This endpoint uses connection tracking, including STOMP's direct AMQP
        # connections. The statistics-backed /connections list can omit them.
        await self.request("DELETE", "connections/username/" + quote(username, safe=""))

    async def revoke(self, node_id):
        username = f"node_{node_id}"
        await self.request("DELETE", f"permissions/visa/{username}", missing_ok=True)
        await self.disconnect(username)
        await self.request("DELETE", f"users/{username}", missing_ok=True)


class BrokerRuntime:
    def __init__(self, config, repository, service):
        self.config, self.repository, self.service = config, repository, service
        self.ready = False
        self.connection = None
        self.tasks = []

    async def start(self):
        self.connection = await aio_pika.connect_robust(self.config.amqp_url, timeout=15, heartbeat=20)
        channel = await self.connection.channel(publisher_confirms=True, on_return_raises=True)
        await channel.set_qos(prefetch_count=20)
        self.exchanges = {}
        for name in ("visa.commands", "visa.control", "visa.slots"):
            self.exchanges[name] = await channel.declare_exchange(name, aio_pika.ExchangeType.DIRECT, durable=True)
        queue = await channel.declare_queue("visa.coordinator.inbox", durable=True,
                                            arguments={"x-queue-type": "classic"})
        await queue.bind(self.exchanges["visa.commands"], "coordinator")
        await queue.consume(self.consume)
        self.ready = True
        self.tasks = [asyncio.create_task(self.publish_loop()), asyncio.create_task(self.presence_loop())]

    async def close(self):
        self.ready = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.connection:
            await self.connection.close()

    async def consume(self, incoming):
        try:
            message = parse_message(incoming.body)
            if (incoming.routing_key != "coordinator" or not incoming.user_id
                    or incoming.message_id != str(message.message_id) or incoming.type != message.type):
                raise ProtocolError("INVALID_MESSAGE", "AMQP metadata does not match the command")
            result = self.service.handle(message, publisher=incoming.user_id, now=datetime.now(timezone.utc))
            if result.status == "rejected":
                log.warning("Rejected command: %s", result.reason)
        except ProtocolError as error:
            # Never log the body or credentials. Malformed commands are permanent.
            log.warning("Rejected command: %s", error.code)
            try:
                identity = UUID(incoming.message_id)
                if str(identity) == incoming.message_id and incoming.user_id:
                    self.service.reject_invalid(publisher=incoming.user_id, message_id=identity,
                                                error=error, now=datetime.now(timezone.utc))
            except (ValueError, TypeError):
                pass
            except Exception:
                log.error("Could not persist command rejection; delivery will be retried")
                await incoming.reject(requeue=True)
                return
            await incoming.reject(requeue=False)
            return
        except Exception:
            log.error("Command transaction failed; delivery will be retried")
            await incoming.reject(requeue=True)
            await asyncio.sleep(1)
            return
        await incoming.ack()

    async def publish_loop(self):
        while True:
            try:
                for entry in self.repository.pending(now=datetime.now(timezone.utc)):
                    remaining = entry.expiration_ms(datetime.now(timezone.utc))
                    if remaining == 0:
                        continue
                    event = entry.message
                    await self.exchanges[entry.exchange].publish(aio_pika.Message(
                        event.to_json().encode("utf-8"), content_type="application/json",
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT, message_id=str(event.message_id),
                        type=event.type, user_id=self.config.broker_user,
                        expiration=remaining / 1000 if remaining is not None else None),
                        routing_key=entry.routing_key, mandatory=True, timeout=15)
                    self.repository.mark_published(event.message_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("Outbox publication deferred (%s)", type(error).__name__)
                await asyncio.sleep(2)
            await asyncio.sleep(.25)

    async def presence_loop(self):
        while True:
            try:
                self.service.expire(now=datetime.now(timezone.utc))
            except Exception as error:
                log.error("Presence transaction deferred (%s)", type(error).__name__)
            await asyncio.sleep(5)
