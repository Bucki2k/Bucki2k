"""Cliente Stratum v1 (el protocolo que usan los pools de Bitcoin).

Un hilo lector recibe líneas JSON del pool y las convierte en eventos que se
dejan en ``client.events`` (una ``queue.Queue``):

  ("notify", params)            nuevo trabajo (mining.notify)
  ("set_difficulty", valor)     nueva dificultad de share
  ("set_extranonce", params)    nuevo extranonce1 / tamaño de extranonce2
  ("reconnect", params)         el pool pide reconectar
  ("message", texto)            mensaje del pool para el usuario
  ("response", id, result, error)  respuesta a una petición nuestra
  ("disconnected", motivo)      se cerró la conexión
"""

from __future__ import annotations

import json
import queue
import socket
import ssl
import threading
import time

from . import AGENT


class StratumError(Exception):
    """Error de protocolo o rechazo del pool."""


def pool_text(value, limit: int = 200) -> str:
    """Texto enviado por el pool, sin caracteres de control (no puede borrar ni falsear líneas)."""
    return "".join(c if c.isprintable() else "?" for c in str(value))[:limit]


def parse_pool_url(url: str) -> tuple[str, int, bool]:
    """Acepta ``host:puerto``, ``stratum+tcp://host:puerto`` o ``stratum+ssl://host:puerto``."""
    text = url.strip()
    use_tls = False
    if "://" in text:
        scheme, text = text.split("://", 1)
        scheme = scheme.lower()
        if scheme in ("stratum+ssl", "stratum+tls", "ssl", "tls"):
            use_tls = True
        elif scheme not in ("stratum+tcp", "tcp", "stratum"):
            raise ValueError(f"esquema de pool desconocido: {scheme}")
    text = text.rstrip("/")
    host, sep, port_text = text.rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise ValueError("el pool debe tener la forma host:puerto (p. ej. public-pool.io:3333)")
    port = int(port_text)
    if not 0 < port < 65536:
        raise ValueError("puerto no válido")
    try:
        host.encode("idna")
    except UnicodeError:
        raise ValueError(f"nombre de pool no válido: {host!r}") from None
    return host, port, use_tls


class StratumClient:
    def __init__(self, host: str, port: int, use_tls: bool = False, *,
                 connect_timeout: float = 10.0, idle_timeout: float = 180.0):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.connect_timeout = connect_timeout
        # public-pool manda trabajo cada 60 s y ckpool cada 30 s; si no llega
        # nada en este tiempo damos la conexión por muerta.
        self.idle_timeout = idle_timeout
        self.events: queue.Queue = queue.Queue()
        self.extranonce1 = b""
        self.extranonce2_size = 0
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._waiters: dict[int, list] = {}
        self._waiters_lock = threading.Lock()
        self._next_id = 1
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None

    # --- conexión ------------------------------------------------------------

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        if self.use_tls:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(1.0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock = sock
        self._reader = threading.Thread(target=self._read_loop, name="stratum-lector", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closed.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=3)
        self._fail_waiters("conexión cerrada")

    # --- envío -----------------------------------------------------------------

    def _send(self, message: dict) -> None:
        data = (json.dumps(message) + "\n").encode()
        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise ConnectionError("no hay conexión con el pool")
            sock.sendall(data)

    def request(self, method: str, params: list) -> int:
        """Envía una petición sin esperar la respuesta (llegará como evento)."""
        with self._waiters_lock:
            request_id = self._next_id
            self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        return request_id

    def call(self, method: str, params: list, timeout: float = 30.0):
        """Envía una petición y espera su respuesta."""
        slot: list = [threading.Event(), None, None]
        with self._waiters_lock:
            request_id = self._next_id
            self._next_id += 1
            self._waiters[request_id] = slot
        try:
            self._send({"id": request_id, "method": method, "params": params})
            # Esperas cortas: en Windows Ctrl+C no interrumpe una espera larga.
            deadline = time.monotonic() + timeout
            while not slot[0].wait(0.25):
                if time.monotonic() >= deadline:
                    raise StratumError(f"el pool no respondió a {method}")
        finally:
            with self._waiters_lock:
                self._waiters.pop(request_id, None)
        if slot[2] is not None:
            raise StratumError(f"{method}: {format_error(slot[2])}")
        return slot[1]

    def handshake(self, username: str, password: str,
                  suggest_difficulty: float | None = None) -> None:
        result = self.call("mining.subscribe", [AGENT])
        if not isinstance(result, list) or len(result) < 3:
            raise StratumError(f"respuesta inesperada a mining.subscribe: {pool_text(result)}")
        self._set_extranonce(result[1], result[2])
        value = None
        if suggest_difficulty:
            # ckpool solo acepta enteros aquí.
            value = int(suggest_difficulty) if float(suggest_difficulty).is_integer() else suggest_difficulty
            # public-pool solo hace caso a la primera sugerencia, y antes de autorizar
            # evita que empiece con dificultad 100000.
            self.request("mining.suggest_difficulty", [value])
        authorized = self.call("mining.authorize", [username, password])
        if authorized is not True:
            raise StratumError("el pool rechazó el usuario (¿dirección de wallet no válida para este pool?)")
        if value is not None:
            # ckpool ignora la sugerencia si llega antes de autorizar.
            self.request("mining.suggest_difficulty", [value])

    def submit(self, username: str, job_id: str, extranonce2: str, ntime: str, nonce: str) -> int:
        return self.request("mining.submit", [username, job_id, extranonce2, ntime, nonce])

    def _set_extranonce(self, extranonce1, extranonce2_size) -> None:
        if (not isinstance(extranonce1, str) or isinstance(extranonce2_size, bool)
                or not isinstance(extranonce2_size, int)):
            raise StratumError("extranonce no válido")
        if not 1 <= extranonce2_size <= 32:
            raise StratumError(f"tamaño de extranonce2 no soportado: {extranonce2_size}")
        try:
            extranonce1_bytes = parse_hex(extranonce1)
        except ValueError:
            raise StratumError(f"extranonce1 no válido: {pool_text(extranonce1, 40)}") from None
        if len(extranonce1_bytes) > 32:
            raise StratumError("extranonce1 demasiado largo")
        self.extranonce1 = extranonce1_bytes
        self.extranonce2_size = extranonce2_size

    # --- lectura -----------------------------------------------------------------

    def _read_loop(self) -> None:
        buffer = b""
        last_data = time.monotonic()
        reason = "conexión cerrada"
        try:
            while not self._closed.is_set():
                sock = self._sock
                if sock is None:
                    break
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    if time.monotonic() - last_data > self.idle_timeout:
                        reason = f"el pool no envía nada desde hace {int(self.idle_timeout)} s"
                        break
                    continue
                if not chunk:
                    reason = "el pool cerró la conexión"
                    break
                last_data = time.monotonic()
                buffer += chunk
                if len(buffer) > 1_000_000 and b"\n" not in buffer:
                    reason = "el pool envió una línea demasiado larga"
                    break
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._handle_line(line)
                    except Exception as exc:  # mensaje raro del pool: ignorarlo, no cortar
                        self.events.put(("message", f"mensaje no válido ignorado ({type(exc).__name__})"))
        except (OSError, ValueError) as exc:
            if not self._closed.is_set():
                reason = f"error de red: {exc}"
        finally:
            self._fail_waiters(reason)
            if not self._closed.is_set():
                self.events.put(("disconnected", reason))

    def _fail_waiters(self, reason: str) -> None:
        with self._waiters_lock:
            waiters = list(self._waiters.values())
        for slot in waiters:
            if not slot[0].is_set():
                slot[2] = reason
                slot[0].set()

    def _handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            self.events.put(("message", f"línea no válida: {pool_text(line[:200])}"))
            return
        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        if not isinstance(request_id, (int, str, type(None))) or isinstance(request_id, bool):
            return
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, list):
            params = []
        if method:
            if isinstance(method, str):
                self._handle_method(request_id, method, params)
            return
        with self._waiters_lock:
            slot = self._waiters.get(request_id)
        if slot is not None:
            slot[1] = message.get("result")
            slot[2] = message.get("error")
            slot[0].set()
        else:
            self.events.put(("response", request_id, message.get("result"), message.get("error")))

    def _handle_method(self, request_id, method: str, params) -> None:
        if method == "mining.notify":
            self.events.put(("notify", params))
        elif method == "mining.set_difficulty":
            self.events.put(("set_difficulty", params[0] if params else None))
        elif method == "mining.set_extranonce":
            self.events.put(("set_extranonce", params))
        elif method == "client.reconnect":
            self.events.put(("reconnect", params))
        elif method == "client.show_message":
            self.events.put(("message", pool_text(params[0]) if params else ""))
        elif method == "client.get_version":
            self._reply(request_id, AGENT)
        elif method == "mining.ping":
            self._reply(request_id, "pong")
        elif request_id is not None:
            self._reply(request_id, None, [20, "unsupported method", None])

    def _reply(self, request_id, result, error=None) -> None:
        if request_id is None:
            return
        try:
            self._send({"id": request_id, "result": result, "error": error})
        except OSError:
            pass


def format_error(error) -> str:
    if isinstance(error, list) and len(error) >= 2:
        return f"{pool_text(error[1])} (código {pool_text(error[0], 20)})"
    if isinstance(error, dict):
        return pool_text(error.get("message") or error)
    return pool_text(error)


def parse_hex(value, nbytes: int | None = None) -> bytes:
    """Hex estricto (sin espacios) y, si se indica, con longitud exacta en bytes."""
    if not isinstance(value, str) or len(value) % 2 or not all(c in "0123456789abcdefABCDEF" for c in value):
        raise ValueError("hex no válido")
    data = bytes.fromhex(value)
    if nbytes is not None and len(data) != nbytes:
        raise ValueError(f"se esperaban {nbytes} bytes")
    return data
