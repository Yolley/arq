import asyncio
import functools
import logging
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from operator import attrgetter
from typing import Any, Callable, Generator, List, Optional, Tuple, Union
from urllib.parse import urlparse
from uuid import uuid4

from pydantic.validators import make_arbitrary_type_validator
from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.sentinel import Sentinel
from redis.exceptions import RedisError, WatchError

from .constants import default_queue_name, job_key_prefix, result_key_prefix
from .jobs import Deserializer, Job, JobDef, JobResult, Serializer, deserialize_job, serialize_job
from .utils import timestamp_ms, to_ms, to_unix_ms

logger = logging.getLogger('arq.connections')


class SSLContext(ssl.SSLContext):
    """
    Required to avoid problems with
    """

    @classmethod
    def __get_validators__(cls) -> Generator[Callable[..., Any], None, None]:
        yield make_arbitrary_type_validator(ssl.SSLContext)


@dataclass
class RedisSettings:
    """
    No-Op class used to hold redis connection redis_settings.

    Used by :func:`arq.connections.create_pool` and :class:`arq.worker.Worker`.
    """

    host: Union[str, List[Tuple[str, int]]] = 'localhost'
    port: int = 6379
    database: int = 0
    password: Optional[str] = None
    ssl: Union[bool, None, SSLContext] = None
    conn_timeout: int = 1
    conn_retries: int = 5
    conn_retry_delay: int = 1

    sentinel: bool = False
    sentinel_master: str = 'mymaster'

    @classmethod
    def from_dsn(cls, dsn: str) -> 'RedisSettings':
        conf = urlparse(dsn)
        assert conf.scheme in {'redis', 'rediss'}, 'invalid DSN scheme'
        return RedisSettings(
            host=conf.hostname or 'localhost',
            port=conf.port or 6379,
            ssl=conf.scheme == 'rediss',
            password=conf.password,
            database=int((conf.path or '0').strip('/')),
        )

    def __repr__(self) -> str:
        return 'RedisSettings({})'.format(', '.join(f'{k}={v!r}' for k, v in self.__dict__.items()))


# extra time after the job is expected to start when the job key should expire, 1 day in ms
expires_extra_ms = 86_400_000


class ArqRedis(Redis):  # type: ignore[misc]
    """
    Thin subclass of ``redis.asyncio.Redis`` which adds :func:`arq.connections.enqueue_job`.

    :param redis_settings: an instance of ``arq.connections.RedisSettings``.
    :param job_serializer: a function that serializes Python objects to bytes, defaults to pickle.dumps
    :param job_deserializer: a function that deserializes bytes into Python objects, defaults to pickle.loads
    :param default_queue_name: the default queue name to use, defaults to ``arq.queue``.
    :param kwargs: keyword arguments directly passed to ``redis.asyncio.Redis``.
    """

    def __init__(
        self,
        pool_or_conn: Optional[ConnectionPool] = None,
        job_serializer: Optional[Serializer] = None,
        job_deserializer: Optional[Deserializer] = None,
        default_queue_name: str = default_queue_name,
        **kwargs: Any,
    ) -> None:
        self.job_serializer = job_serializer
        self.job_deserializer = job_deserializer
        self.default_queue_name = default_queue_name
        if pool_or_conn:
            kwargs['connection_pool'] = pool_or_conn
        super().__init__(**kwargs)

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: Optional[str] = None,
        _queue_name: Optional[str] = None,
        _defer_until: Optional[datetime] = None,
        _defer_by: Union[None, int, float, timedelta] = None,
        _expires: Union[None, int, float, timedelta] = None,
        _job_try: Optional[int] = None,
        **kwargs: Any,
    ) -> Optional[Job]:
        """
        Enqueue a job.

        :param function: Name of the function to call
        :param args: args to pass to the function
        :param _job_id: ID of the job, can be used to enforce job uniqueness
        :param _queue_name: queue of the job, can be used to create job in different queue
        :param _defer_until: datetime at which to run the job
        :param _defer_by: duration to wait before running the job
        :param _expires: if the job still hasn't started after this duration, do not run it
        :param _job_try: useful when re-enqueueing jobs within a job
        :param kwargs: any keyword arguments to pass to the function
        :return: :class:`arq.jobs.Job` instance or ``None`` if a job with this ID already exists
        """
        if _queue_name is None:
            _queue_name = self.default_queue_name
        job_id = _job_id or uuid4().hex
        job_key = job_key_prefix + job_id
        assert not (_defer_until and _defer_by), "use either 'defer_until' or 'defer_by' or neither, not both"

        defer_by_ms = to_ms(_defer_by)
        expires_ms = to_ms(_expires)

        async with self.pipeline(transaction=True) as pipe:
            await pipe.watch(job_key)
            if any(await asyncio.gather(pipe.exists(job_key), pipe.exists(result_key_prefix + job_id))):
                await pipe.reset()
                return None

            enqueue_time_ms = timestamp_ms()
            if _defer_until is not None:
                score = to_unix_ms(_defer_until)
            elif defer_by_ms:
                score = enqueue_time_ms + defer_by_ms
            else:
                score = enqueue_time_ms

            expires_ms = expires_ms or score - enqueue_time_ms + expires_extra_ms

            job = serialize_job(function, args, kwargs, _job_try, enqueue_time_ms, serializer=self.job_serializer)
            pipe.multi()
            pipe.psetex(job_key, expires_ms, job)
            pipe.zadd(_queue_name, {job_id: score})
            try:
                await pipe.execute()
            except WatchError:
                # job got enqueued since we checked 'job_exists'
                return None
        return Job(job_id, redis=self, _queue_name=_queue_name, _deserializer=self.job_deserializer)

    async def _get_job_result(self, key: bytes) -> JobResult:
        job_id = key[len(result_key_prefix) :].decode()
        job = Job(job_id, self, _deserializer=self.job_deserializer)
        r = await job.result_info()
        if r is None:
            raise KeyError(f'job "{key.decode()}" not found')
        r.job_id = job_id
        return r

    async def all_job_results(self) -> List[JobResult]:
        """
        Get results for all jobs in redis.
        """
        keys = await self.keys(result_key_prefix + '*')
        results = await asyncio.gather(*[self._get_job_result(k) for k in keys])
        return sorted(results, key=attrgetter('enqueue_time'))

    async def _get_job_def(self, job_id: bytes, score: int) -> JobDef:
        v = await self.get(job_key_prefix + job_id.decode())
        jd = deserialize_job(v, deserializer=self.job_deserializer)
        jd.score = score
        return jd

    async def queued_jobs(self, *, queue_name: str = default_queue_name) -> List[JobDef]:
        """
        Get information about queued, mostly useful when testing.
        """
        jobs = await self.zrange(queue_name, withscores=True, start=0, end=-1)
        return await asyncio.gather(*[self._get_job_def(job_id, int(score)) for job_id, score in jobs])


async def create_pool(
    settings_: RedisSettings = None,
    *,
    retry: int = 0,
    job_serializer: Optional[Serializer] = None,
    job_deserializer: Optional[Deserializer] = None,
    default_queue_name: str = default_queue_name,
) -> ArqRedis:
    """
    Create a new redis pool, retrying up to ``conn_retries`` times if the connection fails.

    Returns a :class:`arq.connections.ArqRedis` instance, thus allowing job enqueuing.
    """
    settings: RedisSettings = RedisSettings() if settings_ is None else settings_

    assert not (
        type(settings.host) is str and settings.sentinel
    ), "str provided for 'host' but 'sentinel' is true; list of sentinels expected"

    if settings.sentinel:

        def pool_factory(*args: Any, **kwargs: Any) -> ArqRedis:
            client = Sentinel(*args, sentinels=settings.host, ssl=settings.ssl, **kwargs)
            return client.master_for(settings.sentinel_master, redis_class=ArqRedis)

    else:
        pool_factory = functools.partial(
            ArqRedis,
            host=settings.host,
            port=settings.port,
            socket_connect_timeout=settings.conn_timeout,
            ssl=settings.ssl,
        )

    try:
        pool = pool_factory(db=settings.database, password=settings.password, encoding='utf8')
        pool.job_serializer = job_serializer
        pool.job_deserializer = job_deserializer
        pool.default_queue_name = default_queue_name
        await pool.ping()

    except (ConnectionError, OSError, RedisError, asyncio.TimeoutError) as e:
        if retry < settings.conn_retries:
            logger.warning(
                'redis connection error %s:%s %s %s, %d retries remaining...',
                settings.host,
                settings.port,
                e.__class__.__name__,
                e,
                settings.conn_retries - retry,
            )
            await asyncio.sleep(settings.conn_retry_delay)
        else:
            raise
    else:
        if retry > 0:
            logger.info('redis connection successful')
        return pool

    # recursively attempt to create the pool outside the except block to avoid
    # "During handling of the above exception..." madness
    return await create_pool(
        settings,
        retry=retry + 1,
        job_serializer=job_serializer,
        job_deserializer=job_deserializer,
        default_queue_name=default_queue_name,
    )


async def log_redis_info(redis: Redis, log_func: Callable[[str], Any]) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.info(section='Server')
        pipe.info(section='Memory')
        pipe.info(section='Clients')
        pipe.dbsize()
        info_server, info_memory, info_clients, key_count = await pipe.execute()

    redis_version = info_server.get('redis_version', '?')
    mem_usage = info_memory.get('used_memory_human', '?')
    clients_connected = info_clients.get('connected_clients', '?')

    log_func(
        f'redis_version={redis_version} '
        f'mem_usage={mem_usage} '
        f'clients_connected={clients_connected} '
        f'db_keys={key_count}'
    )
