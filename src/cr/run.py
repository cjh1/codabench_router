import asyncio
from asyncio.exceptions import CancelledError
import logging
import json
import sys
from typing import List
import copy
from pathlib import Path

import aio_pika
import aio_pika.abc
import coloredlogs
import httpx
from pydantic import BaseModel, TypeAdapter, ConfigDict
import tenacity
from watchfiles import awatch, Change

from cr.config import settings
from cr.config import CodaBenchQueue

logger = logging.getLogger("cr")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = coloredlogs.ColoredFormatter(
    "%(asctime)s,%(msecs)03d - %(name)s - %(levelname)s - %(message)s"
)
handler.setFormatter(formatter)
logger.addHandler(handler)


class User(BaseModel):
    id: int
    username: str
    is_bot: bool
    email: str
    status: str


class Submission(BaseModel):
    model_config = ConfigDict(extra="allow")

    owner: str


@tenacity.retry(
    retry=tenacity.retry_if_exception_type(httpx.NetworkError)
    | tenacity.retry_if_exception_type(httpx.TimeoutException),
    wait=tenacity.wait_exponential(max=10),
    stop=tenacity.stop_after_attempt(10),
)
async def _fetch_submission(submission_id: int) -> Submission:
    async with httpx.AsyncClient(base_url=str(settings.CODABENCH_URL)) as client:
        competition = settings.CODABENCH_COMPETITION
        token = settings.CODABENCH_API_TOKEN
        headers = {"Authorization": f"Token {token}"}

        r = await client.get(f"/api/submissions/{submission_id}/", headers=headers)
        r.raise_for_status()

        return Submission.model_validate(r.json())


@tenacity.retry(
    retry=tenacity.retry_if_exception_type(httpx.NetworkError)
    | tenacity.retry_if_exception_type(httpx.TimeoutException),
    wait=tenacity.wait_exponential(max=10),
    stop=tenacity.stop_after_attempt(10),
)
async def _fetch_participants():
    async with httpx.AsyncClient(base_url=str(settings.CODABENCH_URL)) as client:
        competition = settings.CODABENCH_COMPETITION
        token = settings.CODABENCH_API_TOKEN
        headers = {"Authorization": f"Token {token}"}
        r = await client.get(
            f"/api/participants/?competition={competition}", headers=headers
        )
        r.raise_for_status()

        ta = TypeAdapter(List[User])
        participants = ta.validate_python(r.json())

        return {p.username: p for p in participants}


async def _route_message(message):
    async with message.process():
        data = message.body
        body = json.loads(data.decode())
        headers = message.headers
        time_limit = headers["timelimit"]

        # We need coerce to float, as they come in as int and get encoded as
        # the wrong type.
        headers["timelimit"] = [float(l) if l is not None else l for l in time_limit]

        run_args = body[0][0]
        user_id = run_args["user_pk"]
        submission_id = run_args["id"]

        logger.info(f"Task submitted by user_id: {user_id}")

        submission = await _fetch_submission(submission_id)
        owner = submission.owner

        logger.info(f"Submission ID: {submission_id}")
        logger.info(f"Submission owner: {owner}")

        target_queue = settings.DEFAULT_TARGET_QUEUE_NAME

        participants = await _fetch_participants()

        if owner not in participants:
            logger.warning(f"User not in participant list: {owner}")
            return

        participant = participants.get(owner)

        routing_key = str(getattr(participant, settings.CODABENCH_USER_ROUTING_KEY))

        target_queue_name = settings.DEFAULT_TARGET_QUEUE_NAME

        if routing_key in settings.ROUTES:
            target_queue_name = settings.ROUTES[routing_key]
            logger.info(f"Using routing key: '{routing_key}'")

        target_queue = settings.QUEUES[target_queue_name]
        logger.info(f"Routing task for '{owner}' to queue '{target_queue_name}'")

        async with await aio_pika.connect_robust(
            str(target_queue.rabbitmq_broker_url)
        ) as worker_connection:
            async with await worker_connection.channel() as worker_channel:
                worker_exchange = await worker_channel.declare_exchange(
                    target_queue.rabbitmq_exchange,
                    auto_delete=False,
                    durable=True,
                )
                worker_queue = await worker_channel.declare_queue(
                    target_queue.rabbitmq_queue,
                    durable=True,
                    arguments={"x-max-priority": 10},
                )

                await worker_queue.bind(worker_exchange, target_queue.rabbitmq_queue)

                await worker_exchange.publish(
                    aio_pika.Message(
                        body=data, content_type="application/json", headers=headers
                    ),
                    routing_key=target_queue.rabbitmq_routing_key,
                    timeout=5,
                )


def _update_routing(file: Path):
    logger.info(f"Loading routing file: {file}")
    with file.open("r") as fp:
        keys = fp.readlines()

    # Remove whitespace
    keys = [k.strip() for k in keys]
    # Remove empties
    keys = [k for k in keys if k]

    target = file.stem

    # Clean up old route
    current = copy.deepcopy(settings.ROUTES)

    for k in list(current.keys()):
        if current[k] == target:
            del current[k]

    for k in keys:
        current[k] = target

    settings.ROUTES = current

    logger.info(f"New routes installed: {settings.ROUTES}")


async def _watch_routing_file(file: Path):
    # watch the routing file for changes
    async for changes in awatch(file, force_polling=True, poll_delay_ms=1000):
        logger.info("Routing file modified.")
        ((change_type, _),) = changes
        if change_type == Change.modified:
            _update_routing(file)


async def init_routing():
    # Now look at the routing file
    if settings.ROUTING_FILE is not None:
        _update_routing(settings.ROUTING_FILE)

        asyncio.create_task(_watch_routing_file(settings.ROUTING_FILE))


async def _broadcast_pidbox_messages(
    queue: aio_pika.abc.AbstractQueue, exchanges: List[aio_pika.Exchange]
):
    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                for e in exchanges:
                    await e.publish(
                        message,
                        "",
                    )


async def route_pidbox(source_queue: CodaBenchQueue):
    from_broker_url = str(source_queue.rabbitmq_broker_url)
    pidbox_exchange_name = source_queue.rabbitmq_pidbox_exchange
    loop = asyncio.get_event_loop()

    while True:
        # Setup connections and exchanges we need to forward messages to
        connections = []
        exchanges = []
        queues = [settings.QUEUES[v] for v in set(settings.ROUTES.values())]
        for q in queues:
            connection = await aio_pika.connect_robust(
                str(q.rabbitmq_broker_url), loop=loop
            )
            connections.append(connection)
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                pidbox_exchange_name, type="fanout", auto_delete=False
            )
            exchanges.append(exchange)

        # Now connect to our source queue
        try:
            async with await aio_pika.connect_robust(
                from_broker_url,
                loop=loop,
            ) as connection:
                async with await connection.channel() as channel:
                    exchange = await channel.declare_exchange(
                        pidbox_exchange_name, type="fanout", auto_delete=False
                    )

                    queue: aio_pika.abc.AbstractQueue = await channel.declare_queue(
                        "pidbox_relay", durable=True, auto_delete=True
                    )
                    await queue.bind(exchange)
                    await _broadcast_pidbox_messages(queue, exchanges)
        except CancelledError:
            return
        except Exception as ex:
            logger.exception("Exception from routing loop")
            logger.info("Sleep and then try again")
            await asyncio.sleep(10)
        finally:
            for c in connections:
                await c.close()


async def route():
    pidbox_task = None

    await init_routing()

    loop = asyncio.get_event_loop()

    source_queue_name = settings.SOURCE_QUEUE_NAME
    default_target_queue_name = settings.DEFAULT_TARGET_QUEUE_NAME
    routing_key = settings.CODABENCH_USER_ROUTING_KEY

    logger.info(f"SOURCE_QUEUE: {source_queue_name}")
    logger.info(f"DEFAULT_TARGET_QUEUE_NAME: {default_target_queue_name}")
    logger.info(f"CODABENCH_USER_ROUTING_KEY: {routing_key.value}")

    source_queue = settings.QUEUES[source_queue_name]

    logger.info("Starting pidbox routing loop")
    pidbox_task = asyncio.create_task(route_pidbox(source_queue))

    while True:
        try:
            async with await aio_pika.connect_robust(
                str(source_queue.rabbitmq_broker_url),
                loop=loop,
            ) as connection:
                async with await connection.channel() as channel:
                    queue: aio_pika.abc.AbstractQueue = await channel.declare_queue(
                        source_queue.rabbitmq_queue,
                        durable=True,
                        # arguments={"x-max-priority": 10},
                    )

                    async with queue.iterator() as queue_iter:
                        async for message in queue_iter:
                            await _route_message(message)

        except KeyboardInterrupt:
            return
        except CancelledError:
            return
        except Exception as ex:
            logger.exception("Exception from routing loop")
            logger.info("Sleep and then try again")
            await asyncio.sleep(10)
        finally:
            if pidbox_task is not None:
                pidbox_task.cancel()


def main():
    asyncio.run(route())


if __name__ == "__main__":
    main()
