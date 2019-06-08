import asyncio
import functools
import logging
import re
import signal
import ssl
import sys
import threading

__all__ = ["Bot", "command", "log", "periodic", "regex", "require_admin"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

MAX_RECV_BYTES = 8192


class Trigger:
    __slots__ = "channel", "nick", "message", "group"

    def __init__(self, channel, nick, message, group=None):
        self.channel = channel
        self.nick = nick
        self.message = message
        self.group = group


class Bot:
    _attrs = []
    _actions = []
    _periodic_tasks = []
    _subclasses = set()

    def __init__(self, hostname, nick, channels,
                 password=None, cert=None, port=6697, admins=None):
        self.hostname = hostname
        self.nick = nick
        self.channels = channels
        self.password = password
        self.cert = cert
        self.port = port
        self.admins = admins

        if self.admins is None:
            self.admins = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        cls._subclasses.add(cls)

        _bot_actions = []
        _bot_periodic_tasks = []
        for attr, value in cls.__dict__.items():
            if attr in ["__annotations__", "__doc__", "__module__"]:
                continue

            if attr in cls._attrs:
                log.error(f"Attribute '{attr}' already exists")
                sys.exit(1)

            cls._attrs.append(attr)

            if hasattr(value, "_registered") and value._registered:
                cls._actions.append(attr)

                _bot_actions.append(attr)

            if hasattr(value, "_periodic") and value._periodic:
                cls._periodic_tasks.append(attr)

                _bot_periodic_tasks.append(attr)

        if _bot_actions:
            log.info(
                f"{cls.__name__} actions: "
                f"[{', '.join(_bot_actions)}]"
            )

        if _bot_periodic_tasks:
            log.info(
                f"{cls.__name__} periodic tasks: "
                f"[{', '.join(_bot_periodic_tasks)}]"
            )

    def _setup(self):
        self._context = ssl.create_default_context()

        try:
            self._context.load_verify_locations(self.cert)
        except Exception as e:
            log.exception(e)
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE

        self._loop = asyncio.get_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever)
        self._loop_thread.start()
        asyncio.get_child_watcher().attach_loop(self._loop)

        self._run_coroutine = functools.partial(
            asyncio.run_coroutine_threadsafe,
            loop=self._loop,
        )

    async def _connect(self):
        self._reader, self._writer = await asyncio.open_connection(
            host=self.hostname,
            port=self.port,
            ssl=self._context,
        )

        await self._register()
        await self._set_nick()

        if self.password:
            await self._identify()

        self._run_coroutine(self._join_channels())

    async def _reconnect(self):
        self._writer.close()
        await self._writer.wait_closed()
        await self._connect()

    async def _handle_error_response(self, text):
        if text.startswith("ERROR"):
            log.error(f"Error received from {self.hostname}. Reconnecting...")
            await self._reconnect()

    async def _reply_to_ping(self, text):
        if text.startswith("PING"):
            self._writer.write(f"PONG :{self.hostname}\r\n".encode())

    async def _rejoin_when_kicked(self, text):
        for channel in self.channels:
            if f'KICK {channel} {self.nick}' in text:
                await self._join(channel)

    async def _recv(self):
        text = await self._reader.read(MAX_RECV_BYTES)
        text = text.decode().strip()

        return text

    async def _process_message(self, trigger):
        for action in self._actions:
            await getattr(self, action)(trigger)

    async def _process_line(self, text):
        nick = self._get_nick(text)
        message = self._get_message(text)

        if not message.startswith(f"{self.nick}:"):
            return

        message = message.lstrip(f"{self.nick}:").strip()

        for channel in self.channels:
            if f"PRIVMSG {channel}" in text:
                trigger = Trigger(channel, nick, message)
                await self._process_message(trigger)

    async def _process_text(self, text):
        for line in text.split("\r\n"):
            log.debug(line)

            await self._reply_to_ping(line)
            await self._handle_error_response(line)
            await self._rejoin_when_kicked(line)

            await self._process_line(line)

    async def _irc_bot(self):
        await self._connect()

        while True:
            text = await self._recv()
            await self._process_text(text)

    async def _gather_periodic_tasks(self):
        periodic_tasks = [
            getattr(self, periodic_task)()
            for periodic_task in self._periodic_tasks
        ]
        await asyncio.gather(*periodic_tasks)

    def run(self):
        self._run_coroutine(self._irc_bot())
        self._run_coroutine(self._gather_periodic_tasks())

        try:
            signal.pause()
        except KeyboardInterrupt:
            print("Interrupted")

    async def _say(self, message, channel):
        if message:
            self._writer.write(
                f"PRIVMSG {channel} :{message}\r\n".encode()
            )

    async def say(self, message, channel=None):
        if channel:
            await self._say(message, channel)
        else:
            for channel in self.channels:
                await self._say(message, channel)

    async def run_in_executor(self, func, *args):
        return await self._loop.run_in_executor(None, func, *args)

    async def _register(self):
        self._writer.write(
            f"USER {self.nick} 8 * :{self.nick}\r\n".encode()
        )

    async def _set_nick(self):
        self._writer.write(f"NICK {self.nick}\r\n".encode())

    async def _identify(self):
        self._writer.write(
            f"PRIVMSG NickServ :IDENTIFY {self.password}\r\n".encode()
        )

    async def _join_channels(self, sleep_before_join=20):
        await asyncio.sleep(sleep_before_join)

        for channel in self.channels:
            await self._join(channel)

    async def _join(self, channel):
        self._writer.write(f"JOIN {channel}\r\n".encode())

    def _get_nick(self, text):
        return text.partition("!")[0].lstrip(":")

    def _get_message(self, text):
        return text.partition("PRIVMSG")[2].partition(":")[2]

    @property
    def _bots(self):
        return sorted(
            bot.__name__ for bot in self._subclasses
            if bot.__name__ != Bot.__name__
        )

    def __enter__(self):
        self._setup()
        return self

    def __exit__(self, *args):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join()

    @classmethod
    def create(cls):
        return type(cls.__name__, tuple(cls._subclasses), {})


def command(command):
    def _decorator(func):
        func._registered = True

        @functools.wraps(func)
        async def _wrapper(bot, trigger):
            if command == trigger.message:
                bot._run_coroutine(func(bot, trigger))

        return _wrapper

    return _decorator


def regex(regex):
    def _decorator(func):
        func._registered = True

        @functools.wraps(func)
        async def _wrapper(bot, trigger):
            match = re.match(regex, trigger.message)

            if match:
                trigger.group = match.group
                bot._run_coroutine(func(bot, trigger))

        return _wrapper

    return _decorator


def periodic(sleep_time):
    def _decorator(func):
        func._periodic = True

        @functools.wraps(func)
        async def _wrapper(self_):
            while True:
                await asyncio.sleep(sleep_time)

                try:
                    await func(self_)
                except Exception as e:
                    log.exception(e)

        return _wrapper

    return _decorator


def require_admin(func):
    @functools.wraps(func)
    async def _wrapper(self_, trigger):
        if trigger.nick in self_.admins:
            return await func(self_, trigger)

    return _wrapper
