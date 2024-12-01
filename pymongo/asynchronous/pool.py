# Copyright 2011-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

from __future__ import annotations

import asyncio
import collections
import contextlib
import functools
import logging
import os
import socket
import ssl
import sys
import time
import weakref
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Mapping,
    MutableMapping,
    NoReturn,
    Optional,
    Sequence,
    Union,
)

from bson import DEFAULT_CODEC_OPTIONS
from pymongo import _csot, helpers_shared
from pymongo.asynchronous.client_session import _validate_session_write_concern
from pymongo.asynchronous.helpers import _handle_reauth
from pymongo.asynchronous.network import command, receive_message
from pymongo.common import (
    MAX_BSON_SIZE,
    MAX_MESSAGE_SIZE,
    MAX_WIRE_VERSION,
    MAX_WRITE_BATCH_SIZE,
    ORDERED_TYPES,
)
from pymongo.errors import (  # type:ignore[attr-defined]
    AutoReconnect,
    ConfigurationError,
    ConnectionFailure,
    DocumentTooLarge,
    ExecutionTimeout,
    InvalidOperation,
    NetworkTimeout,
    NotPrimaryError,
    OperationFailure,
    PyMongoError,
    WaitQueueTimeoutError,
    _CertificateError,
)
from pymongo.hello import Hello, HelloCompat
from pymongo.lock import (
    _async_cond_wait,
    _async_create_condition,
    _async_create_lock,
)
from pymongo.logger import (
    _CONNECTION_LOGGER,
    _ConnectionStatusMessage,
    _debug_log,
    _verbose_connection_error_reason,
)
from pymongo.monitoring import (
    ConnectionCheckOutFailedReason,
    ConnectionClosedReason,
)
from pymongo.network_layer import async_sendall
from pymongo.pool_options import PoolOptions
from pymongo.read_preferences import ReadPreference
from pymongo.server_api import _add_to_command
from pymongo.server_type import SERVER_TYPE
from pymongo.socket_checker import SocketChecker
from pymongo.ssl_support import HAS_SNI, SSLError

if TYPE_CHECKING:
    from bson import CodecOptions
    from bson.objectid import ObjectId
    from pymongo.asynchronous.auth import _AuthContext
    from pymongo.asynchronous.client_session import AsyncClientSession
    from pymongo.asynchronous.mongo_client import AsyncMongoClient, _MongoClientErrorHandler
    from pymongo.compression_support import (
        SnappyContext,
        ZlibContext,
        ZstdContext,
    )
    from pymongo.message import _OpMsg, _OpReply
    from pymongo.pyopenssl_context import _sslConn
    from pymongo.read_concern import ReadConcern
    from pymongo.read_preferences import _ServerMode
    from pymongo.typings import ClusterTime, _Address, _CollationIn
    from pymongo.write_concern import WriteConcern

try:
    from fcntl import F_GETFD, F_SETFD, FD_CLOEXEC, fcntl

    def _set_non_inheritable_non_atomic(fd: int) -> None:
        """Set the close-on-exec flag on the given file descriptor."""
        flags = fcntl(fd, F_GETFD)
        fcntl(fd, F_SETFD, flags | FD_CLOEXEC)

except ImportError:
    # Windows, various platforms we don't claim to support
    # (Jython, IronPython, ..), systems that don't provide
    # everything we need from fcntl, etc.
    def _set_non_inheritable_non_atomic(fd: int) -> None:  # noqa: ARG001
        """Dummy function for platforms that don't provide fcntl."""


_IS_SYNC = False

_MAX_TCP_KEEPIDLE = 120
_MAX_TCP_KEEPINTVL = 10
_MAX_TCP_KEEPCNT = 9

if sys.platform == "win32":
    try:
        import _winreg as winreg
    except ImportError:
        import winreg

    def _query(key, name, default):
        try:
            value, _ = winreg.QueryValueEx(key, name)
            # Ensure the value is a number or raise ValueError.
            return int(value)
        except (OSError, ValueError):
            # QueryValueEx raises OSError when the key does not exist (i.e.
            # the system is using the Windows default value).
            return default

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
        ) as key:
            _WINDOWS_TCP_IDLE_MS = _query(key, "KeepAliveTime", 7200000)
            _WINDOWS_TCP_INTERVAL_MS = _query(key, "KeepAliveInterval", 1000)
    except OSError:
        # We could not check the default values because winreg.OpenKey failed.
        # Assume the system is using the default values.
        _WINDOWS_TCP_IDLE_MS = 7200000
        _WINDOWS_TCP_INTERVAL_MS = 1000

    def _set_keepalive_times(sock):
        idle_ms = min(_WINDOWS_TCP_IDLE_MS, _MAX_TCP_KEEPIDLE * 1000)
        interval_ms = min(_WINDOWS_TCP_INTERVAL_MS, _MAX_TCP_KEEPINTVL * 1000)
        if idle_ms < _WINDOWS_TCP_IDLE_MS or interval_ms < _WINDOWS_TCP_INTERVAL_MS:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle_ms, interval_ms))

else:

    def _set_tcp_option(sock: socket.socket, tcp_option: str, max_value: int) -> None:
        if hasattr(socket, tcp_option):
            sockopt = getattr(socket, tcp_option)
            try:
                # PYTHON-1350 - NetBSD doesn't implement getsockopt for
                # TCP_KEEPIDLE and friends. Don't attempt to set the
                # values there.
                default = sock.getsockopt(socket.IPPROTO_TCP, sockopt)
                if default > max_value:
                    sock.setsockopt(socket.IPPROTO_TCP, sockopt, max_value)
            except OSError:
                pass

    def _set_keepalive_times(sock: socket.socket) -> None:
        _set_tcp_option(sock, "TCP_KEEPIDLE", _MAX_TCP_KEEPIDLE)
        _set_tcp_option(sock, "TCP_KEEPINTVL", _MAX_TCP_KEEPINTVL)
        _set_tcp_option(sock, "TCP_KEEPCNT", _MAX_TCP_KEEPCNT)


def _raise_connection_failure(
    address: Any,
    error: Exception,
    msg_prefix: Optional[str] = None,
    timeout_details: Optional[dict[str, float]] = None,
) -> NoReturn:
    """Convert a socket.error to ConnectionFailure and raise it."""
    host, port = address
    # If connecting to a Unix socket, port will be None.
    if port is not None:
        msg = "%s:%d: %s" % (host, port, error)
    else:
        msg = f"{host}: {error}"
    if msg_prefix:
        msg = msg_prefix + msg
    if "configured timeouts" not in msg:
        msg += format_timeout_details(timeout_details)
    if isinstance(error, socket.timeout):
        raise NetworkTimeout(msg) from error
    elif isinstance(error, SSLError) and "timed out" in str(error):
        # Eventlet does not distinguish TLS network timeouts from other
        # SSLErrors (https://github.com/eventlet/eventlet/issues/692).
        # Luckily, we can work around this limitation because the phrase
        # 'timed out' appears in all the timeout related SSLErrors raised.
        raise NetworkTimeout(msg) from error
    else:
        raise AutoReconnect(msg) from error


def _get_timeout_details(options: PoolOptions) -> dict[str, float]:
    details = {}
    timeout = _csot.get_timeout()
    socket_timeout = options.socket_timeout
    connect_timeout = options.connect_timeout
    if timeout:
        details["timeoutMS"] = timeout * 1000
    if socket_timeout and not timeout:
        details["socketTimeoutMS"] = socket_timeout * 1000
    if connect_timeout:
        details["connectTimeoutMS"] = connect_timeout * 1000
    return details


def format_timeout_details(details: Optional[dict[str, float]]) -> str:
    result = ""
    if details:
        result += " (configured timeouts:"
        for timeout in ["socketTimeoutMS", "timeoutMS", "connectTimeoutMS"]:
            if timeout in details:
                result += f" {timeout}: {details[timeout]}ms,"
        result = result[:-1]
        result += ")"
    return result


class _CancellationContext:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Cancel this context."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Was cancel called?"""
        return self._cancelled


class AsyncConnection:
    """Store a connection with some metadata.

    :param conn: a raw connection object
    :param pool: a Pool instance
    :param address: the server's (host, port)
    :param id: the id of this socket in it's pool
    """

    def __init__(
        self, conn: Union[socket.socket, _sslConn], pool: Pool, address: tuple[str, int], id: int
    ):
        self.pool_ref = weakref.ref(pool)
        self.conn = conn
        self.address = address
        self.id = id
        self.closed = False
        self.last_checkin_time = time.monotonic()
        self.performed_handshake = False
        self.is_writable: bool = False
        self.max_wire_version = MAX_WIRE_VERSION
        self.max_bson_size = MAX_BSON_SIZE
        self.max_message_size = MAX_MESSAGE_SIZE
        self.max_write_batch_size = MAX_WRITE_BATCH_SIZE
        self.supports_sessions = False
        self.hello_ok: bool = False
        self.is_mongos = False
        self.op_msg_enabled = False
        self.listeners = pool.opts._event_listeners
        self.enabled_for_cmap = pool.enabled_for_cmap
        self.enabled_for_logging = pool.enabled_for_logging
        self.compression_settings = pool.opts._compression_settings
        self.compression_context: Union[SnappyContext, ZlibContext, ZstdContext, None] = None
        self.socket_checker: SocketChecker = SocketChecker()
        self.oidc_token_gen_id: Optional[int] = None
        # Support for mechanism negotiation on the initial handshake.
        self.negotiated_mechs: Optional[list[str]] = None
        self.auth_ctx: Optional[_AuthContext] = None

        # The pool's generation changes with each reset() so we can close
        # sockets created before the last reset.
        self.pool_gen = pool.gen
        self.generation = self.pool_gen.get_overall()
        self.ready = False
        self.cancel_context: _CancellationContext = _CancellationContext()
        self.opts = pool.opts
        self.more_to_come: bool = False
        # For load balancer support.
        self.service_id: Optional[ObjectId] = None
        self.server_connection_id: Optional[int] = None
        # When executing a transaction in load balancing mode, this flag is
        # set to true to indicate that the session now owns the connection.
        self.pinned_txn = False
        self.pinned_cursor = False
        self.active = False
        self.last_timeout = self.opts.socket_timeout
        self.connect_rtt = 0.0
        self._client_id = pool._client_id
        self.creation_time = time.monotonic()

    def set_conn_timeout(self, timeout: Optional[float]) -> None:
        """Cache last timeout to avoid duplicate calls to conn.settimeout."""
        if timeout == self.last_timeout:
            return
        self.last_timeout = timeout
        self.conn.settimeout(timeout)

    def apply_timeout(
        self, client: AsyncMongoClient, cmd: Optional[MutableMapping[str, Any]]
    ) -> Optional[float]:
        # CSOT: use remaining timeout when set.
        timeout = _csot.remaining()
        if timeout is None:
            # Reset the socket timeout unless we're performing a streaming monitor check.
            if not self.more_to_come:
                self.set_conn_timeout(self.opts.socket_timeout)
            return None
        # RTT validation.
        rtt = _csot.get_rtt()
        if rtt is None:
            rtt = self.connect_rtt
        max_time_ms = timeout - rtt
        if max_time_ms < 0:
            timeout_details = _get_timeout_details(self.opts)
            formatted = format_timeout_details(timeout_details)
            # CSOT: raise an error without running the command since we know it will time out.
            errmsg = f"operation would exceed time limit, remaining timeout:{timeout:.5f} <= network round trip time:{rtt:.5f} {formatted}"
            raise ExecutionTimeout(
                errmsg,
                50,
                {"ok": 0, "errmsg": errmsg, "code": 50},
                self.max_wire_version,
            )
        if cmd is not None:
            cmd["maxTimeMS"] = int(max_time_ms * 1000)
        self.set_conn_timeout(timeout)
        return timeout

    def pin_txn(self) -> None:
        self.pinned_txn = True
        assert not self.pinned_cursor

    def pin_cursor(self) -> None:
        self.pinned_cursor = True
        assert not self.pinned_txn

    async def unpin(self) -> None:
        pool = self.pool_ref()
        if pool:
            await pool.checkin(self)
        else:
            self.close_conn(ConnectionClosedReason.STALE)

    def hello_cmd(self) -> dict[str, Any]:
        # Handshake spec requires us to use OP_MSG+hello command for the
        # initial handshake in load balanced or stable API mode.
        if self.opts.server_api or self.hello_ok or self.opts.load_balanced:
            self.op_msg_enabled = True
            return {HelloCompat.CMD: 1}
        else:
            return {HelloCompat.LEGACY_CMD: 1, "helloOk": True}

    async def hello(self) -> Hello:
        return await self._hello(None, None, None)

    async def _hello(
        self,
        cluster_time: Optional[ClusterTime],
        topology_version: Optional[Any],
        heartbeat_frequency: Optional[int],
    ) -> Hello[dict[str, Any]]:
        cmd = self.hello_cmd()
        performing_handshake = not self.performed_handshake
        awaitable = False
        if performing_handshake:
            self.performed_handshake = True
            cmd["client"] = self.opts.metadata
            if self.compression_settings:
                cmd["compression"] = self.compression_settings.compressors
            if self.opts.load_balanced:
                cmd["loadBalanced"] = True
        elif topology_version is not None:
            cmd["topologyVersion"] = topology_version
            assert heartbeat_frequency is not None
            cmd["maxAwaitTimeMS"] = int(heartbeat_frequency * 1000)
            awaitable = True
            # If connect_timeout is None there is no timeout.
            if self.opts.connect_timeout:
                self.set_conn_timeout(self.opts.connect_timeout + heartbeat_frequency)

        if not performing_handshake and cluster_time is not None:
            cmd["$clusterTime"] = cluster_time

        creds = self.opts._credentials
        if creds:
            if creds.mechanism == "DEFAULT" and creds.username:
                cmd["saslSupportedMechs"] = creds.source + "." + creds.username
            from pymongo.asynchronous import auth

            auth_ctx = auth._AuthContext.from_credentials(creds, self.address)
            if auth_ctx:
                speculative_authenticate = auth_ctx.speculate_command()
                if speculative_authenticate is not None:
                    cmd["speculativeAuthenticate"] = speculative_authenticate
        else:
            auth_ctx = None

        if performing_handshake:
            start = time.monotonic()
        doc = await self.command("admin", cmd, publish_events=False, exhaust_allowed=awaitable)
        if performing_handshake:
            self.connect_rtt = time.monotonic() - start
        hello = Hello(doc, awaitable=awaitable)
        self.is_writable = hello.is_writable
        self.max_wire_version = hello.max_wire_version
        self.max_bson_size = hello.max_bson_size
        self.max_message_size = hello.max_message_size
        self.max_write_batch_size = hello.max_write_batch_size
        self.supports_sessions = (
            hello.logical_session_timeout_minutes is not None and hello.is_readable
        )
        self.logical_session_timeout_minutes: Optional[int] = hello.logical_session_timeout_minutes
        self.hello_ok = hello.hello_ok
        self.is_repl = hello.server_type in (
            SERVER_TYPE.RSPrimary,
            SERVER_TYPE.RSSecondary,
            SERVER_TYPE.RSArbiter,
            SERVER_TYPE.RSOther,
            SERVER_TYPE.RSGhost,
        )
        self.is_standalone = hello.server_type == SERVER_TYPE.Standalone
        self.is_mongos = hello.server_type == SERVER_TYPE.Mongos
        if performing_handshake and self.compression_settings:
            ctx = self.compression_settings.get_compression_context(hello.compressors)
            self.compression_context = ctx

        self.op_msg_enabled = True
        self.server_connection_id = hello.connection_id
        if creds:
            self.negotiated_mechs = hello.sasl_supported_mechs
        if auth_ctx:
            auth_ctx.parse_response(hello)  # type:ignore[arg-type]
            if auth_ctx.speculate_succeeded():
                self.auth_ctx = auth_ctx
        if self.opts.load_balanced:
            if not hello.service_id:
                raise ConfigurationError(
                    "Driver attempted to initialize in load balancing mode,"
                    " but the server does not support this mode"
                )
            self.service_id = hello.service_id
            self.generation = self.pool_gen.get(self.service_id)
        return hello

    async def _next_reply(self) -> dict[str, Any]:
        reply = await self.receive_message(None)
        self.more_to_come = reply.more_to_come
        unpacked_docs = reply.unpack_response()
        response_doc = unpacked_docs[0]
        helpers_shared._check_command_response(response_doc, self.max_wire_version)
        return response_doc

    @_handle_reauth
    async def command(
        self,
        dbname: str,
        spec: MutableMapping[str, Any],
        read_preference: _ServerMode = ReadPreference.PRIMARY,
        codec_options: CodecOptions = DEFAULT_CODEC_OPTIONS,
        check: bool = True,
        allowable_errors: Optional[Sequence[Union[str, int]]] = None,
        read_concern: Optional[ReadConcern] = None,
        write_concern: Optional[WriteConcern] = None,
        parse_write_concern_error: bool = False,
        collation: Optional[_CollationIn] = None,
        session: Optional[AsyncClientSession] = None,
        client: Optional[AsyncMongoClient] = None,
        retryable_write: bool = False,
        publish_events: bool = True,
        user_fields: Optional[Mapping[str, Any]] = None,
        exhaust_allowed: bool = False,
    ) -> dict[str, Any]:
        """Execute a command or raise an error.

        :param dbname: name of the database on which to run the command
        :param spec: a command document as a dict, SON, or mapping object
        :param read_preference: a read preference
        :param codec_options: a CodecOptions instance
        :param check: raise OperationFailure if there are errors
        :param allowable_errors: errors to ignore if `check` is True
        :param read_concern: The read concern for this command.
        :param write_concern: The write concern for this command.
        :param parse_write_concern_error: Whether to parse the
            ``writeConcernError`` field in the command response.
        :param collation: The collation for this command.
        :param session: optional AsyncClientSession instance.
        :param client: optional AsyncMongoClient for gossipping $clusterTime.
        :param retryable_write: True if this command is a retryable write.
        :param publish_events: Should we publish events for this command?
        :param user_fields: Response fields that should be decoded
            using the TypeDecoders from codec_options, passed to
            bson._decode_all_selective.
        """
        self.validate_session(client, session)
        session = _validate_session_write_concern(session, write_concern)

        # Ensure command name remains in first place.
        if not isinstance(spec, ORDERED_TYPES):  # type:ignore[arg-type]
            spec = dict(spec)

        if not (write_concern is None or write_concern.acknowledged or collation is None):
            raise ConfigurationError("Collation is unsupported for unacknowledged writes.")

        self.add_server_api(spec)
        if session:
            session._apply_to(spec, retryable_write, read_preference, self)
        self.send_cluster_time(spec, session, client)
        listeners = self.listeners if publish_events else None
        unacknowledged = bool(write_concern and not write_concern.acknowledged)
        if self.op_msg_enabled:
            self._raise_if_not_writable(unacknowledged)
        try:
            return await command(
                self,
                dbname,
                spec,
                self.is_mongos,
                read_preference,
                codec_options,
                session,
                client,
                check,
                allowable_errors,
                self.address,
                listeners,
                self.max_bson_size,
                read_concern,
                parse_write_concern_error=parse_write_concern_error,
                collation=collation,
                compression_ctx=self.compression_context,
                use_op_msg=self.op_msg_enabled,
                unacknowledged=unacknowledged,
                user_fields=user_fields,
                exhaust_allowed=exhaust_allowed,
                write_concern=write_concern,
            )
        except (OperationFailure, NotPrimaryError):
            raise
        # Catch socket.error, KeyboardInterrupt, etc. and close ourselves.
        except BaseException as error:
            self._raise_connection_failure(error)

    async def send_message(self, message: bytes, max_doc_size: int) -> None:
        """Send a raw BSON message or raise ConnectionFailure.

        If a network exception is raised, the socket is closed.
        """
        if self.max_bson_size is not None and max_doc_size > self.max_bson_size:
            raise DocumentTooLarge(
                "BSON document too large (%d bytes) - the connected server "
                "supports BSON document sizes up to %d bytes." % (max_doc_size, self.max_bson_size)
            )

        try:
            await async_sendall(self.conn, message)
        except BaseException as error:
            self._raise_connection_failure(error)

    async def receive_message(self, request_id: Optional[int]) -> Union[_OpReply, _OpMsg]:
        """Receive a raw BSON message or raise ConnectionFailure.

        If any exception is raised, the socket is closed.
        """
        try:
            return await receive_message(self, request_id, self.max_message_size)
        except BaseException as error:
            self._raise_connection_failure(error)

    def _raise_if_not_writable(self, unacknowledged: bool) -> None:
        """Raise NotPrimaryError on unacknowledged write if this socket is not
        writable.
        """
        if unacknowledged and not self.is_writable:
            # Write won't succeed, bail as if we'd received a not primary error.
            raise NotPrimaryError("not primary", {"ok": 0, "errmsg": "not primary", "code": 10107})

    async def unack_write(self, msg: bytes, max_doc_size: int) -> None:
        """Send unack OP_MSG.

        Can raise ConnectionFailure or InvalidDocument.

        :param msg: bytes, an OP_MSG message.
        :param max_doc_size: size in bytes of the largest document in `msg`.
        """
        self._raise_if_not_writable(True)
        await self.send_message(msg, max_doc_size)

    async def write_command(
        self, request_id: int, msg: bytes, codec_options: CodecOptions
    ) -> dict[str, Any]:
        """Send "insert" etc. command, returning response as a dict.

        Can raise ConnectionFailure or OperationFailure.

        :param request_id: an int.
        :param msg: bytes, the command message.
        """
        await self.send_message(msg, 0)
        reply = await self.receive_message(request_id)
        result = reply.command_response(codec_options)

        # Raises NotPrimaryError or OperationFailure.
        helpers_shared._check_command_response(result, self.max_wire_version)
        return result

    async def authenticate(self, reauthenticate: bool = False) -> None:
        """Authenticate to the server if needed.

        Can raise ConnectionFailure or OperationFailure.
        """
        # CMAP spec says to publish the ready event only after authenticating
        # the connection.
        if reauthenticate:
            if self.performed_handshake:
                # Existing auth_ctx is stale, remove it.
                self.auth_ctx = None
            self.ready = False
        if not self.ready:
            creds = self.opts._credentials
            if creds:
                from pymongo.asynchronous import auth

                await auth.authenticate(creds, self, reauthenticate=reauthenticate)
            self.ready = True
            duration = time.monotonic() - self.creation_time
            if self.enabled_for_cmap:
                assert self.listeners is not None
                self.listeners.publish_connection_ready(self.address, self.id, duration)
            if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                _debug_log(
                    _CONNECTION_LOGGER,
                    clientId=self._client_id,
                    message=_ConnectionStatusMessage.CONN_READY,
                    serverHost=self.address[0],
                    serverPort=self.address[1],
                    driverConnectionId=self.id,
                    durationMS=duration,
                )

    def validate_session(
        self, client: Optional[AsyncMongoClient], session: Optional[AsyncClientSession]
    ) -> None:
        """Validate this session before use with client.

        Raises error if the client is not the one that created the session.
        """
        if session:
            if session._client is not client:
                raise InvalidOperation(
                    "Can only use session with the AsyncMongoClient that started it"
                )

    def close_conn(self, reason: Optional[str]) -> None:
        """Close this connection with a reason."""
        if self.closed:
            return
        self._close_conn()
        if reason:
            if self.enabled_for_cmap:
                assert self.listeners is not None
                self.listeners.publish_connection_closed(self.address, self.id, reason)
            if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                _debug_log(
                    _CONNECTION_LOGGER,
                    clientId=self._client_id,
                    message=_ConnectionStatusMessage.CONN_CLOSED,
                    serverHost=self.address[0],
                    serverPort=self.address[1],
                    driverConnectionId=self.id,
                    reason=_verbose_connection_error_reason(reason),
                    error=reason,
                )

    def _close_conn(self) -> None:
        """Close this connection."""
        if self.closed:
            return
        self.closed = True
        self.cancel_context.cancel()
        # Note: We catch exceptions to avoid spurious errors on interpreter
        # shutdown.
        try:
            self.conn.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: S110
            pass

    def conn_closed(self) -> bool:
        """Return True if we know socket has been closed, False otherwise."""
        return self.socket_checker.socket_closed(self.conn)

    def send_cluster_time(
        self,
        command: MutableMapping[str, Any],
        session: Optional[AsyncClientSession],
        client: Optional[AsyncMongoClient],
    ) -> None:
        """Add $clusterTime."""
        if client:
            client._send_cluster_time(command, session)

    def add_server_api(self, command: MutableMapping[str, Any]) -> None:
        """Add server_api parameters."""
        if self.opts.server_api:
            _add_to_command(command, self.opts.server_api)

    def update_last_checkin_time(self) -> None:
        self.last_checkin_time = time.monotonic()

    def update_is_writable(self, is_writable: bool) -> None:
        self.is_writable = is_writable

    def idle_time_seconds(self) -> float:
        """Seconds since this socket was last checked into its pool."""
        return time.monotonic() - self.last_checkin_time

    def _raise_connection_failure(self, error: BaseException) -> NoReturn:
        # Catch *all* exceptions from socket methods and close the socket. In
        # regular Python, socket operations only raise socket.error, even if
        # the underlying cause was a Ctrl-C: a signal raised during socket.recv
        # is expressed as an EINTR error from poll. See internal_select_ex() in
        # socketmodule.c. All error codes from poll become socket.error at
        # first. Eventually in PyEval_EvalFrameEx the interpreter checks for
        # signals and throws KeyboardInterrupt into the current frame on the
        # main thread.
        #
        # But in Gevent and Eventlet, the polling mechanism (epoll, kqueue,
        # ..) is called in Python code, which experiences the signal as a
        # KeyboardInterrupt from the start, rather than as an initial
        # socket.error, so we catch that, close the socket, and reraise it.
        #
        # The connection closed event will be emitted later in checkin.
        if self.ready:
            reason = None
        else:
            reason = ConnectionClosedReason.ERROR
        self.close_conn(reason)
        # SSLError from PyOpenSSL inherits directly from Exception.
        if isinstance(error, (IOError, OSError, SSLError)):
            details = _get_timeout_details(self.opts)
            _raise_connection_failure(self.address, error, timeout_details=details)
        else:
            raise

    def __eq__(self, other: Any) -> bool:
        return self.conn == other.conn

    def __ne__(self, other: Any) -> bool:
        return not self == other

    def __hash__(self) -> int:
        return hash(self.conn)

    def __repr__(self) -> str:
        return "AsyncConnection({}){} at {}".format(
            repr(self.conn),
            self.closed and " CLOSED" or "",
            id(self),
        )


def _create_connection(address: _Address, options: PoolOptions) -> socket.socket:
    """Given (host, port) and PoolOptions, connect and return a socket object.

    Can raise socket.error.

    This is a modified version of create_connection from CPython >= 2.7.
    """
    host, port = address

    # Check if dealing with a unix domain socket
    if host.endswith(".sock"):
        if not hasattr(socket, "AF_UNIX"):
            raise ConnectionFailure("UNIX-sockets are not supported on this system")
        sock = socket.socket(socket.AF_UNIX)
        # SOCK_CLOEXEC not supported for Unix sockets.
        _set_non_inheritable_non_atomic(sock.fileno())
        try:
            sock.connect(host)
            return sock
        except OSError:
            sock.close()
            raise

    # Don't try IPv6 if we don't support it. Also skip it if host
    # is 'localhost' (::1 is fine). Avoids slow connect issues
    # like PYTHON-356.
    family = socket.AF_INET
    if socket.has_ipv6 and host != "localhost":
        family = socket.AF_UNSPEC

    err = None
    for res in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
        af, socktype, proto, dummy, sa = res
        # SOCK_CLOEXEC was new in CPython 3.2, and only available on a limited
        # number of platforms (newer Linux and *BSD). Starting with CPython 3.4
        # all file descriptors are created non-inheritable. See PEP 446.
        try:
            sock = socket.socket(af, socktype | getattr(socket, "SOCK_CLOEXEC", 0), proto)
        except OSError:
            # Can SOCK_CLOEXEC be defined even if the kernel doesn't support
            # it?
            sock = socket.socket(af, socktype, proto)
        # Fallback when SOCK_CLOEXEC isn't available.
        _set_non_inheritable_non_atomic(sock.fileno())
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # CSOT: apply timeout to socket connect.
            timeout = _csot.remaining()
            if timeout is None:
                timeout = options.connect_timeout
            elif timeout <= 0:
                raise socket.timeout("timed out")
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, True)
            _set_keepalive_times(sock)
            sock.connect(sa)
            return sock
        except OSError as e:
            err = e
            sock.close()

    if err is not None:
        raise err
    else:
        # This likely means we tried to connect to an IPv6 only
        # host with an OS/kernel or Python interpreter that doesn't
        # support IPv6. The test case is Jython2.5.1 which doesn't
        # support IPv6 at all.
        raise OSError("getaddrinfo failed")


async def _configured_socket(
    address: _Address, options: PoolOptions
) -> Union[socket.socket, _sslConn]:
    """Given (host, port) and PoolOptions, return a configured socket.

    Can raise socket.error, ConnectionFailure, or _CertificateError.

    Sets socket's SSL and timeout options.
    """
    sock = _create_connection(address, options)
    ssl_context = options._ssl_context

    if ssl_context is None:
        sock.settimeout(options.socket_timeout)
        return sock

    host = address[0]
    try:
        # We have to pass hostname / ip address to wrap_socket
        # to use SSLContext.check_hostname.
        if HAS_SNI:
            if _IS_SYNC:
                ssl_sock = ssl_context.wrap_socket(sock, server_hostname=host)
            else:
                if hasattr(ssl_context, "a_wrap_socket"):
                    ssl_sock = await ssl_context.a_wrap_socket(sock, server_hostname=host)  # type: ignore[assignment, misc]
                else:
                    loop = asyncio.get_running_loop()
                    ssl_sock = await loop.run_in_executor(
                        None,
                        functools.partial(ssl_context.wrap_socket, sock, server_hostname=host),  # type: ignore[assignment, misc]
                    )
        else:
            if _IS_SYNC:
                ssl_sock = ssl_context.wrap_socket(sock)
            else:
                if hasattr(ssl_context, "a_wrap_socket"):
                    ssl_sock = await ssl_context.a_wrap_socket(sock)  # type: ignore[assignment, misc]
                else:
                    loop = asyncio.get_running_loop()
                    ssl_sock = await loop.run_in_executor(None, ssl_context.wrap_socket, sock)  # type: ignore[assignment, misc]
    except _CertificateError:
        sock.close()
        # Raise _CertificateError directly like we do after match_hostname
        # below.
        raise
    except (OSError, SSLError) as exc:
        sock.close()
        # We raise AutoReconnect for transient and permanent SSL handshake
        # failures alike. Permanent handshake failures, like protocol
        # mismatch, will be turned into ServerSelectionTimeoutErrors later.
        details = _get_timeout_details(options)
        _raise_connection_failure(address, exc, "SSL handshake failed: ", timeout_details=details)
    if (
        ssl_context.verify_mode
        and not ssl_context.check_hostname
        and not options.tls_allow_invalid_hostnames
    ):
        try:
            ssl.match_hostname(ssl_sock.getpeercert(), hostname=host)  # type:ignore[attr-defined]
        except _CertificateError:
            ssl_sock.close()
            raise

    ssl_sock.settimeout(options.socket_timeout)
    return ssl_sock


class _PoolClosedError(PyMongoError):
    """Internal error raised when a thread tries to get a connection from a
    closed pool.
    """


class _PoolGeneration:
    def __init__(self) -> None:
        # Maps service_id to generation.
        self._generations: dict[ObjectId, int] = collections.defaultdict(int)
        # Overall pool generation.
        self._generation = 0

    def get(self, service_id: Optional[ObjectId]) -> int:
        """Get the generation for the given service_id."""
        if service_id is None:
            return self._generation
        return self._generations[service_id]

    def get_overall(self) -> int:
        """Get the Pool's overall generation."""
        return self._generation

    def inc(self, service_id: Optional[ObjectId]) -> None:
        """Increment the generation for the given service_id."""
        self._generation += 1
        if service_id is None:
            for service_id in self._generations:
                self._generations[service_id] += 1
        else:
            self._generations[service_id] += 1

    def stale(self, gen: int, service_id: Optional[ObjectId]) -> bool:
        """Return if the given generation for a given service_id is stale."""
        return gen != self.get(service_id)


class PoolState:
    PAUSED = 1
    READY = 2
    CLOSED = 3


# Do *not* explicitly inherit from object or Jython won't call __del__
# http://bugs.jython.org/issue1057
class Pool:
    def __init__(
        self,
        address: _Address,
        options: PoolOptions,
        handshake: bool = True,
        client_id: Optional[ObjectId] = None,
    ):
        """
        :param address: a (hostname, port) tuple
        :param options: a PoolOptions instance
        :param handshake: whether to call hello for each new AsyncConnection
        """
        if options.pause_enabled:
            self.state = PoolState.PAUSED
        else:
            self.state = PoolState.READY
        # Check a socket's health with socket_closed() every once in a while.
        # Can override for testing: 0 to always check, None to never check.
        self._check_interval_seconds = 1
        # LIFO pool. Sockets are ordered on idle time. Sockets claimed
        # and returned to pool from the left side. Stale sockets removed
        # from the right side.
        self.conns: collections.deque = collections.deque()
        self.active_contexts: set[_CancellationContext] = set()
        self.lock = _async_create_lock()
        self._max_connecting_cond = _async_create_condition(self.lock)
        self.active_sockets = 0
        # Monotonically increasing connection ID required for CMAP Events.
        self.next_connection_id = 1
        # Track whether the sockets in this pool are writeable or not.
        self.is_writable: Optional[bool] = None

        # Keep track of resets, so we notice sockets created before the most
        # recent reset and close them.
        # self.generation = 0
        self.gen = _PoolGeneration()
        self.pid = os.getpid()
        self.address = address
        self.opts = options
        self.handshake = handshake
        # Don't publish events or logs in Monitor pools.
        self.enabled_for_cmap = (
            self.handshake
            and self.opts._event_listeners is not None
            and self.opts._event_listeners.enabled_for_cmap
        )
        self.enabled_for_logging = self.handshake

        # The first portion of the wait queue.
        # Enforces: maxPoolSize
        # Also used for: clearing the wait queue
        self.size_cond = _async_create_condition(self.lock)
        self.requests = 0
        self.max_pool_size = self.opts.max_pool_size
        if not self.max_pool_size:
            self.max_pool_size = float("inf")
        # The second portion of the wait queue.
        # Enforces: maxConnecting
        # Also used for: clearing the wait queue
        self._max_connecting_cond = _async_create_condition(self.lock)
        self._max_connecting = self.opts.max_connecting
        self._pending = 0
        self._client_id = client_id
        if self.enabled_for_cmap:
            assert self.opts._event_listeners is not None
            self.opts._event_listeners.publish_pool_created(
                self.address, self.opts.non_default_options
            )
        if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
            _debug_log(
                _CONNECTION_LOGGER,
                clientId=self._client_id,
                message=_ConnectionStatusMessage.POOL_CREATED,
                serverHost=self.address[0],
                serverPort=self.address[1],
                **self.opts.non_default_options,
            )
        # Similar to active_sockets but includes threads in the wait queue.
        self.operation_count: int = 0
        # Retain references to pinned connections to prevent the CPython GC
        # from thinking that a cursor's pinned connection can be GC'd when the
        # cursor is GC'd (see PYTHON-2751).
        self.__pinned_sockets: set[AsyncConnection] = set()
        self.ncursors = 0
        self.ntxns = 0

    async def ready(self) -> None:
        # Take the lock to avoid the race condition described in PYTHON-2699.
        async with self.lock:
            if self.state != PoolState.READY:
                self.state = PoolState.READY
                if self.enabled_for_cmap:
                    assert self.opts._event_listeners is not None
                    self.opts._event_listeners.publish_pool_ready(self.address)
                if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                    _debug_log(
                        _CONNECTION_LOGGER,
                        clientId=self._client_id,
                        message=_ConnectionStatusMessage.POOL_READY,
                        serverHost=self.address[0],
                        serverPort=self.address[1],
                    )

    @property
    def closed(self) -> bool:
        return self.state == PoolState.CLOSED

    async def _reset(
        self,
        close: bool,
        pause: bool = True,
        service_id: Optional[ObjectId] = None,
        interrupt_connections: bool = False,
    ) -> None:
        old_state = self.state
        async with self.size_cond:
            if self.closed:
                return
            if self.opts.pause_enabled and pause and not self.opts.load_balanced:
                old_state, self.state = self.state, PoolState.PAUSED
            self.gen.inc(service_id)
            newpid = os.getpid()
            if self.pid != newpid:
                self.pid = newpid
                self.active_sockets = 0
                self.operation_count = 0
            if service_id is None:
                sockets, self.conns = self.conns, collections.deque()
            else:
                discard: collections.deque = collections.deque()
                keep: collections.deque = collections.deque()
                for conn in self.conns:
                    if conn.service_id == service_id:
                        discard.append(conn)
                    else:
                        keep.append(conn)
                sockets = discard
                self.conns = keep

            if close:
                self.state = PoolState.CLOSED
            # Clear the wait queue
            self._max_connecting_cond.notify_all()
            self.size_cond.notify_all()

            if interrupt_connections:
                for context in self.active_contexts:
                    context.cancel()

        listeners = self.opts._event_listeners
        # CMAP spec says that close() MUST close sockets before publishing the
        # PoolClosedEvent but that reset() SHOULD close sockets *after*
        # publishing the PoolClearedEvent.
        if close:
            for conn in sockets:
                conn.close_conn(ConnectionClosedReason.POOL_CLOSED)
            if self.enabled_for_cmap:
                assert listeners is not None
                listeners.publish_pool_closed(self.address)
            if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                _debug_log(
                    _CONNECTION_LOGGER,
                    clientId=self._client_id,
                    message=_ConnectionStatusMessage.POOL_CLOSED,
                    serverHost=self.address[0],
                    serverPort=self.address[1],
                )
        else:
            if old_state != PoolState.PAUSED:
                if self.enabled_for_cmap:
                    assert listeners is not None
                    listeners.publish_pool_cleared(
                        self.address,
                        service_id=service_id,
                        interrupt_connections=interrupt_connections,
                    )
                if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                    _debug_log(
                        _CONNECTION_LOGGER,
                        clientId=self._client_id,
                        message=_ConnectionStatusMessage.POOL_CLEARED,
                        serverHost=self.address[0],
                        serverPort=self.address[1],
                        serviceId=service_id,
                    )
            for conn in sockets:
                conn.close_conn(ConnectionClosedReason.STALE)

    async def update_is_writable(self, is_writable: Optional[bool]) -> None:
        """Updates the is_writable attribute on all sockets currently in the
        Pool.
        """
        self.is_writable = is_writable
        async with self.lock:
            for _socket in self.conns:
                _socket.update_is_writable(self.is_writable)

    async def reset(
        self, service_id: Optional[ObjectId] = None, interrupt_connections: bool = False
    ) -> None:
        await self._reset(
            close=False, service_id=service_id, interrupt_connections=interrupt_connections
        )

    async def reset_without_pause(self) -> None:
        await self._reset(close=False, pause=False)

    async def close(self) -> None:
        await self._reset(close=True)

    def stale_generation(self, gen: int, service_id: Optional[ObjectId]) -> bool:
        return self.gen.stale(gen, service_id)

    async def remove_stale_sockets(self, reference_generation: int) -> None:
        """Removes stale sockets then adds new ones if pool is too small and
        has not been reset. The `reference_generation` argument specifies the
        `generation` at the point in time this operation was requested on the
        pool.
        """
        # Take the lock to avoid the race condition described in PYTHON-2699.
        async with self.lock:
            if self.state != PoolState.READY:
                return

        if self.opts.max_idle_time_seconds is not None:
            async with self.lock:
                while (
                    self.conns
                    and self.conns[-1].idle_time_seconds() > self.opts.max_idle_time_seconds
                ):
                    conn = self.conns.pop()
                    conn.close_conn(ConnectionClosedReason.IDLE)

        while True:
            async with self.size_cond:
                # There are enough sockets in the pool.
                if len(self.conns) + self.active_sockets >= self.opts.min_pool_size:
                    return
                if self.requests >= self.opts.min_pool_size:
                    return
                self.requests += 1
            incremented = False
            try:
                async with self._max_connecting_cond:
                    # If maxConnecting connections are already being created
                    # by this pool then try again later instead of waiting.
                    if self._pending >= self._max_connecting:
                        return
                    self._pending += 1
                    incremented = True
                conn = await self.connect()
                async with self.lock:
                    # Close connection and return if the pool was reset during
                    # socket creation or while acquiring the pool lock.
                    if self.gen.get_overall() != reference_generation:
                        conn.close_conn(ConnectionClosedReason.STALE)
                        return
                    self.conns.appendleft(conn)
                    self.active_contexts.discard(conn.cancel_context)
            finally:
                if incremented:
                    # Notify after adding the socket to the pool.
                    async with self._max_connecting_cond:
                        self._pending -= 1
                        self._max_connecting_cond.notify()

                async with self.size_cond:
                    self.requests -= 1
                    self.size_cond.notify()

    async def connect(self, handler: Optional[_MongoClientErrorHandler] = None) -> AsyncConnection:
        """Connect to Mongo and return a new AsyncConnection.

        Can raise ConnectionFailure.

        Note that the pool does not keep a reference to the socket -- you
        must call checkin() when you're done with it.
        """
        async with self.lock:
            conn_id = self.next_connection_id
            self.next_connection_id += 1
            # Use a temporary context so that interrupt_connections can cancel creating the socket.
            tmp_context = _CancellationContext()
            self.active_contexts.add(tmp_context)

        listeners = self.opts._event_listeners
        if self.enabled_for_cmap:
            assert listeners is not None
            listeners.publish_connection_created(self.address, conn_id)
        if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
            _debug_log(
                _CONNECTION_LOGGER,
                clientId=self._client_id,
                message=_ConnectionStatusMessage.CONN_CREATED,
                serverHost=self.address[0],
                serverPort=self.address[1],
                driverConnectionId=conn_id,
            )

        try:
            sock = await _configured_socket(self.address, self.opts)
        except BaseException as error:
            async with self.lock:
                self.active_contexts.discard(tmp_context)
            if self.enabled_for_cmap:
                assert listeners is not None
                listeners.publish_connection_closed(
                    self.address, conn_id, ConnectionClosedReason.ERROR
                )
            if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                _debug_log(
                    _CONNECTION_LOGGER,
                    clientId=self._client_id,
                    message=_ConnectionStatusMessage.CONN_CLOSED,
                    serverHost=self.address[0],
                    serverPort=self.address[1],
                    driverConnectionId=conn_id,
                    reason=_verbose_connection_error_reason(ConnectionClosedReason.ERROR),
                    error=ConnectionClosedReason.ERROR,
                )
            if isinstance(error, (IOError, OSError, SSLError)):
                details = _get_timeout_details(self.opts)
                _raise_connection_failure(self.address, error, timeout_details=details)

            raise

        conn = AsyncConnection(sock, self, self.address, conn_id)  # type: ignore[arg-type]
        async with self.lock:
            self.active_contexts.add(conn.cancel_context)
            self.active_contexts.discard(tmp_context)
        if tmp_context.cancelled:
            conn.cancel_context.cancel()
        try:
            if self.handshake:
                await conn.hello()
                self.is_writable = conn.is_writable
            if handler:
                handler.contribute_socket(conn, completed_handshake=False)

            await conn.authenticate()
        except BaseException:
            async with self.lock:
                self.active_contexts.discard(conn.cancel_context)
            conn.close_conn(ConnectionClosedReason.ERROR)
            raise

        return conn

    @contextlib.asynccontextmanager
    async def checkout(
        self, handler: Optional[_MongoClientErrorHandler] = None
    ) -> AsyncGenerator[AsyncConnection, None]:
        """Get a connection from the pool. Use with a "with" statement.

        Returns a :class:`AsyncConnection` object wrapping a connected
        :class:`socket.socket`.

        This method should always be used in a with-statement::

            with pool.get_conn() as connection:
                connection.send_message(msg)
                data = connection.receive_message(op_code, request_id)

        Can raise ConnectionFailure or OperationFailure.

        :param handler: A _MongoClientErrorHandler.
        """
        listeners = self.opts._event_listeners
        checkout_started_time = time.monotonic()
        if self.enabled_for_cmap:
            assert listeners is not None
            listeners.publish_connection_check_out_started(self.address)
        if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
            _debug_log(
                _CONNECTION_LOGGER,
                clientId=self._client_id,
                message=_ConnectionStatusMessage.CHECKOUT_STARTED,
                serverHost=self.address[0],
                serverPort=self.address[1],
            )

        conn = await self._get_conn(checkout_started_time, handler=handler)

        duration = time.monotonic() - checkout_started_time
        if self.enabled_for_cmap:
            assert listeners is not None
            listeners.publish_connection_checked_out(self.address, conn.id, duration)
        if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
            _debug_log(
                _CONNECTION_LOGGER,
                clientId=self._client_id,
                message=_ConnectionStatusMessage.CHECKOUT_SUCCEEDED,
                serverHost=self.address[0],
                serverPort=self.address[1],
                driverConnectionId=conn.id,
                durationMS=duration,
            )
        try:
            async with self.lock:
                self.active_contexts.add(conn.cancel_context)
            yield conn
        except BaseException:
            # Exception in caller. Ensure the connection gets returned.
            # Note that when pinned is True, the session owns the
            # connection and it is responsible for checking the connection
            # back into the pool.
            pinned = conn.pinned_txn or conn.pinned_cursor
            if handler:
                # Perform SDAM error handling rules while the connection is
                # still checked out.
                exc_type, exc_val, _ = sys.exc_info()
                await handler.handle(exc_type, exc_val)
            if not pinned and conn.active:
                await self.checkin(conn)
            raise
        if conn.pinned_txn:
            async with self.lock:
                self.__pinned_sockets.add(conn)
                self.ntxns += 1
        elif conn.pinned_cursor:
            async with self.lock:
                self.__pinned_sockets.add(conn)
                self.ncursors += 1
        elif conn.active:
            await self.checkin(conn)

    def _raise_if_not_ready(self, checkout_started_time: float, emit_event: bool) -> None:
        if self.state != PoolState.READY:
            if emit_event:
                duration = time.monotonic() - checkout_started_time
                if self.enabled_for_cmap:
                    assert self.opts._event_listeners is not None
                    self.opts._event_listeners.publish_connection_check_out_failed(
                        self.address, ConnectionCheckOutFailedReason.CONN_ERROR, duration
                    )
                if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                    _debug_log(
                        _CONNECTION_LOGGER,
                        clientId=self._client_id,
                        message=_ConnectionStatusMessage.CHECKOUT_FAILED,
                        serverHost=self.address[0],
                        serverPort=self.address[1],
                        reason="An error occurred while trying to establish a new connection",
                        error=ConnectionCheckOutFailedReason.CONN_ERROR,
                        durationMS=duration,
                    )

            details = _get_timeout_details(self.opts)
            _raise_connection_failure(
                self.address, AutoReconnect("connection pool paused"), timeout_details=details
            )

    async def _get_conn(
        self, checkout_started_time: float, handler: Optional[_MongoClientErrorHandler] = None
    ) -> AsyncConnection:
        """Get or create a AsyncConnection. Can raise ConnectionFailure."""
        # We use the pid here to avoid issues with fork / multiprocessing.
        # See test.test_client:TestClient.test_fork for an example of
        # what could go wrong otherwise
        if self.pid != os.getpid():
            await self.reset_without_pause()

        if self.closed:
            duration = time.monotonic() - checkout_started_time
            if self.enabled_for_cmap:
                assert self.opts._event_listeners is not None
                self.opts._event_listeners.publish_connection_check_out_failed(
                    self.address, ConnectionCheckOutFailedReason.POOL_CLOSED, duration
                )
            if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                _debug_log(
                    _CONNECTION_LOGGER,
                    clientId=self._client_id,
                    message=_ConnectionStatusMessage.CHECKOUT_FAILED,
                    serverHost=self.address[0],
                    serverPort=self.address[1],
                    reason="Connection pool was closed",
                    error=ConnectionCheckOutFailedReason.POOL_CLOSED,
                    durationMS=duration,
                )
            raise _PoolClosedError(
                "Attempted to check out a connection from closed connection pool"
            )

        async with self.lock:
            self.operation_count += 1

        # Get a free socket or create one.
        if _csot.get_timeout():
            deadline = _csot.get_deadline()
        elif self.opts.wait_queue_timeout:
            deadline = time.monotonic() + self.opts.wait_queue_timeout
        else:
            deadline = None

        async with self.size_cond:
            self._raise_if_not_ready(checkout_started_time, emit_event=True)
            while not (self.requests < self.max_pool_size):
                timeout = deadline - time.monotonic() if deadline else None
                if not await _async_cond_wait(self.size_cond, timeout):
                    # Timed out, notify the next thread to ensure a
                    # timeout doesn't consume the condition.
                    if self.requests < self.max_pool_size:
                        self.size_cond.notify()
                    self._raise_wait_queue_timeout(checkout_started_time)
                self._raise_if_not_ready(checkout_started_time, emit_event=True)
            self.requests += 1

        # We've now acquired the semaphore and must release it on error.
        conn = None
        incremented = False
        emitted_event = False
        try:
            async with self.lock:
                self.active_sockets += 1
                incremented = True
            while conn is None:
                # CMAP: we MUST wait for either maxConnecting OR for a socket
                # to be checked back into the pool.
                async with self._max_connecting_cond:
                    self._raise_if_not_ready(checkout_started_time, emit_event=False)
                    while not (self.conns or self._pending < self._max_connecting):
                        timeout = deadline - time.monotonic() if deadline else None
                        if not await _async_cond_wait(self._max_connecting_cond, timeout):
                            # Timed out, notify the next thread to ensure a
                            # timeout doesn't consume the condition.
                            if self.conns or self._pending < self._max_connecting:
                                self._max_connecting_cond.notify()
                            emitted_event = True
                            self._raise_wait_queue_timeout(checkout_started_time)
                        self._raise_if_not_ready(checkout_started_time, emit_event=False)

                    try:
                        conn = self.conns.popleft()
                    except IndexError:
                        self._pending += 1
                if conn:  # We got a socket from the pool
                    if self._perished(conn):
                        conn = None
                        continue
                else:  # We need to create a new connection
                    try:
                        conn = await self.connect(handler=handler)
                    finally:
                        async with self._max_connecting_cond:
                            self._pending -= 1
                            self._max_connecting_cond.notify()
        except BaseException:
            if conn:
                # We checked out a socket but authentication failed.
                conn.close_conn(ConnectionClosedReason.ERROR)
            async with self.size_cond:
                self.requests -= 1
                if incremented:
                    self.active_sockets -= 1
                self.size_cond.notify()

            if not emitted_event:
                duration = time.monotonic() - checkout_started_time
                if self.enabled_for_cmap:
                    assert self.opts._event_listeners is not None
                    self.opts._event_listeners.publish_connection_check_out_failed(
                        self.address, ConnectionCheckOutFailedReason.CONN_ERROR, duration
                    )
                if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                    _debug_log(
                        _CONNECTION_LOGGER,
                        clientId=self._client_id,
                        message=_ConnectionStatusMessage.CHECKOUT_FAILED,
                        serverHost=self.address[0],
                        serverPort=self.address[1],
                        reason="An error occurred while trying to establish a new connection",
                        error=ConnectionCheckOutFailedReason.CONN_ERROR,
                        durationMS=duration,
                    )
            raise

        conn.active = True
        return conn

    async def checkin(self, conn: AsyncConnection) -> None:
        """Return the connection to the pool, or if it's closed discard it.

        :param conn: The connection to check into the pool.
        """
        txn = conn.pinned_txn
        cursor = conn.pinned_cursor
        conn.active = False
        conn.pinned_txn = False
        conn.pinned_cursor = False
        self.__pinned_sockets.discard(conn)
        listeners = self.opts._event_listeners
        async with self.lock:
            self.active_contexts.discard(conn.cancel_context)
        if self.enabled_for_cmap:
            assert listeners is not None
            listeners.publish_connection_checked_in(self.address, conn.id)
        if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
            _debug_log(
                _CONNECTION_LOGGER,
                clientId=self._client_id,
                message=_ConnectionStatusMessage.CHECKEDIN,
                serverHost=self.address[0],
                serverPort=self.address[1],
                driverConnectionId=conn.id,
            )
        if self.pid != os.getpid():
            await self.reset_without_pause()
        else:
            if self.closed:
                conn.close_conn(ConnectionClosedReason.POOL_CLOSED)
            elif conn.closed:
                # CMAP requires the closed event be emitted after the check in.
                if self.enabled_for_cmap:
                    assert listeners is not None
                    listeners.publish_connection_closed(
                        self.address, conn.id, ConnectionClosedReason.ERROR
                    )
                if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
                    _debug_log(
                        _CONNECTION_LOGGER,
                        clientId=self._client_id,
                        message=_ConnectionStatusMessage.CONN_CLOSED,
                        serverHost=self.address[0],
                        serverPort=self.address[1],
                        driverConnectionId=conn.id,
                        reason=_verbose_connection_error_reason(ConnectionClosedReason.ERROR),
                        error=ConnectionClosedReason.ERROR,
                    )
            else:
                async with self.lock:
                    # Hold the lock to ensure this section does not race with
                    # Pool.reset().
                    if self.stale_generation(conn.generation, conn.service_id):
                        conn.close_conn(ConnectionClosedReason.STALE)
                    else:
                        conn.update_last_checkin_time()
                        conn.update_is_writable(bool(self.is_writable))
                        self.conns.appendleft(conn)
                        # Notify any threads waiting to create a connection.
                        self._max_connecting_cond.notify()

        async with self.size_cond:
            if txn:
                self.ntxns -= 1
            elif cursor:
                self.ncursors -= 1
            self.requests -= 1
            self.active_sockets -= 1
            self.operation_count -= 1
            self.size_cond.notify()

    def _perished(self, conn: AsyncConnection) -> bool:
        """Return True and close the connection if it is "perished".

        This side-effecty function checks if this socket has been idle for
        for longer than the max idle time, or if the socket has been closed by
        some external network error, or if the socket's generation is outdated.

        Checking sockets lets us avoid seeing *some*
        :class:`~pymongo.errors.AutoReconnect` exceptions on server
        hiccups, etc. We only check if the socket was closed by an external
        error if it has been > 1 second since the socket was checked into the
        pool, to keep performance reasonable - we can't avoid AutoReconnects
        completely anyway.
        """
        idle_time_seconds = conn.idle_time_seconds()
        # If socket is idle, open a new one.
        if (
            self.opts.max_idle_time_seconds is not None
            and idle_time_seconds > self.opts.max_idle_time_seconds
        ):
            conn.close_conn(ConnectionClosedReason.IDLE)
            return True

        if self._check_interval_seconds is not None and (
            self._check_interval_seconds == 0 or idle_time_seconds > self._check_interval_seconds
        ):
            if conn.conn_closed():
                conn.close_conn(ConnectionClosedReason.ERROR)
                return True

        if self.stale_generation(conn.generation, conn.service_id):
            conn.close_conn(ConnectionClosedReason.STALE)
            return True

        return False

    def _raise_wait_queue_timeout(self, checkout_started_time: float) -> NoReturn:
        listeners = self.opts._event_listeners
        duration = time.monotonic() - checkout_started_time
        if self.enabled_for_cmap:
            assert listeners is not None
            listeners.publish_connection_check_out_failed(
                self.address, ConnectionCheckOutFailedReason.TIMEOUT, duration
            )
        if self.enabled_for_logging and _CONNECTION_LOGGER.isEnabledFor(logging.DEBUG):
            _debug_log(
                _CONNECTION_LOGGER,
                clientId=self._client_id,
                message=_ConnectionStatusMessage.CHECKOUT_FAILED,
                serverHost=self.address[0],
                serverPort=self.address[1],
                reason="Wait queue timeout elapsed without a connection becoming available",
                error=ConnectionCheckOutFailedReason.TIMEOUT,
                durationMS=duration,
            )
        timeout = _csot.get_timeout() or self.opts.wait_queue_timeout
        if self.opts.load_balanced:
            other_ops = self.active_sockets - self.ncursors - self.ntxns
            raise WaitQueueTimeoutError(
                "Timeout waiting for connection from the connection pool. "
                "maxPoolSize: {}, connections in use by cursors: {}, "
                "connections in use by transactions: {}, connections in use "
                "by other operations: {}, timeout: {}".format(
                    self.opts.max_pool_size,
                    self.ncursors,
                    self.ntxns,
                    other_ops,
                    timeout,
                )
            )
        raise WaitQueueTimeoutError(
            "Timed out while checking out a connection from connection pool. "
            f"maxPoolSize: {self.opts.max_pool_size}, timeout: {timeout}"
        )

    def __del__(self) -> None:
        # Avoid ResourceWarnings in Python 3
        # Close all sockets without calling reset() or close() because it is
        # not safe to acquire a lock in __del__.
        for conn in self.conns:
            conn.close_conn(None)
