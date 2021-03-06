import asyncio
import functools
import logging
import re
import signal
import ssl
import sys

__all__ = (
    "Bot",
    "command",
    "log",
    "periodic",
    "regex",
    "require_admin",
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

MAX_RECV_BYTES = 8192
TIMEOUT = 600

SOCKET_SERVER_HOST = "127.0.0.1"
SOCKET_SERVER_PORT = 9999


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
                 password=None, cert=None, port=6697, admins=()):
        self.hostname = hostname
        self.nick = nick
        self.channels = channels
        self.password = password
        self.cert = cert
        self.port = port
        self.admins = admins

        self._setup()

    def __new__(cls, *args, **kwargs):
        return super().__new__(type(cls.__name__, tuple(cls._subclasses), {}))

    def _setup(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)

        self._context = ssl.create_default_context()

        try:
            self._context.load_verify_locations(self.cert)
        except Exception as e:
            log.exception(e)
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        cls._subclasses.add(cls)

        _bot_actions = []
        _bot_periodic_tasks = []
        for attr, value in vars(cls).items():
            if attr in ["__doc__", "__module__"]:
                continue

            if attr in cls._attrs:
                log.error("Attribute '%s' already exists", attr)
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
                "%s actions: [%s]",
                cls.__name__,
                ", ".join(_bot_actions)
            )

        if _bot_periodic_tasks:
            log.info(
                "%s periodic tasks: [%s]",
                cls.__name__,
                ", ".join(_bot_periodic_tasks)
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

        asyncio.create_task(self._join_channels())

    async def _close_writer(self):
        self._writer.close()
        await self._writer.wait_closed()

    async def _reconnect(self):
        await self._close_writer()
        await self._connect()

    async def _shutdown(self):
        await self._close_writer()
        sys.exit(1)

    async def _handle_error_response(self, text):
        if text.startswith("ERROR"):
            log.error(
                "Error received from %s. Reconnecting...",
                self.hostname
            )
            await self._reconnect()

    async def _handle_empty_response(self, text):
        if not text:
            log.error("No text received. Shutting down bot...")
            await self._shutdown()

    async def _reply_to_ping(self, text):
        if text.startswith("PING"):
            self._writer.write(f"PONG :{self.hostname}\r\n".encode())

    async def _rejoin_when_kicked(self, text):
        for channel in self.channels:
            if f'KICK {channel} {self.nick}' in text:
                await self._join(channel)

    async def _recv(self):
        text = await self._reader.read(MAX_RECV_BYTES)

        return text.decode(errors="ignore").strip()

    async def _process_message(self, trigger):
        for action in self._actions:
            await getattr(self, action)(trigger)

    async def _process_line(self, text):
        nick = self._get_nick(text)
        message = self._get_message(text)

        if not message.startswith(f"{self.nick}:"):
            return

        message = message.partition(":")[2].strip()

        for channel in self.channels:
            if f"PRIVMSG {channel}" in text:
                trigger = Trigger(channel, nick, message)
                await self._process_message(trigger)

    async def _process_text(self, text):
        for line in text.split("\r\n"):
            log.debug(line)

            await self._reply_to_ping(line)
            await self._handle_error_response(line)
            await self._handle_empty_response(line)
            await self._rejoin_when_kicked(line)

            await self._process_line(line)

    async def _start_irc_bot(self):
        await self._connect()

        while True:
            try:
                text = await asyncio.wait_for(
                    self._recv(),
                    timeout=float(TIMEOUT),
                )
            except asyncio.TimeoutError:
                log.error(
                    "No data received for %d seconds. "
                    "Shutting down bot...",
                    TIMEOUT,
                )
                await self._shutdown()
            else:
                await self._process_text(text)

    async def _start_periodic_tasks(self):
        periodic_tasks = [
            getattr(self, periodic_task)()
            for periodic_task in self._periodic_tasks
        ]
        await asyncio.gather(*periodic_tasks)

    async def _process_socket_server_text(self, text, writer):
        channel, _, message = text.partition(" ")
        if channel in self.channels:
            await self.say(message, channel)
            writer.write(f"'{message}'\n".encode())
        else:
            writer.write(f"'Unknown channel: {channel}'\n".encode())

    async def _socket_server(self, reader, writer):
        host, port = writer._transport._sock.getpeername()

        log.info("New connection from %s:%d", host, port)
        writer.write(
            "Please enter your message starting "
            "with the channel name.\n".encode()
        )

        while True:
            writer.write(">>> ".encode())
            data = await reader.read(100)
            text = data.decode().strip()

            if not text:
                break

            await self._process_socket_server_text(text, writer)

            await writer.drain()

        writer.close()
        await writer.wait_closed()
        log.info("Connection with %s:%d closed", host, port)

    async def _start_socket_server(self, host, port):
        server = await asyncio.start_server(self._socket_server, host, port)
        log.info("Starting socket server on: %s:%d", host, port)
        await server.serve_forever()

    async def _run(self):
        await asyncio.gather(
            self._start_irc_bot(),
            self._start_periodic_tasks(),
            self._start_socket_server(SOCKET_SERVER_HOST, SOCKET_SERVER_PORT),
        )

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

    @staticmethod
    def _get_nick(text):
        return text.partition("!")[0].lstrip(":")

    @staticmethod
    def _get_message(text):
        return text.partition("PRIVMSG")[2].partition(":")[2]

    @property
    def _bots(self):
        return sorted(
            bot.__name__ for bot in self._subclasses
            if bot.__name__ != Bot.__name__
        )

    @staticmethod
    async def run_in_executor(func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    def run(self):
        asyncio.run(self._run())


def command(command):
    def _decorator(func):
        func._registered = True

        @functools.wraps(func)
        async def _wrapper(bot, trigger):
            if command == trigger.message:
                asyncio.create_task(func(bot, trigger))

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
                asyncio.create_task(func(bot, trigger))

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
