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
        raise ValueError("el pool debe tener la forma host:puerto (p. ej. public-pool.io:21496)")
    port = int(port_text)
    if not 0 < port < 65536:
        raise ValueError("puerto no válido")
    return host, port, use_tls


class StratumClient:
    def __init__(self, host: str, port: int, use_tls: bool = False, *,
                 connect_timeout: float = 20.0, idle_timeout: float = 300.0):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.connect_timeout = connect_timeout
        # Los pools mandan trabajo nuevo cada pocos segundos o minutos; si no
        # llega nada en este tiempo damos la conexión por muerta.
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
            if self._sock is None:
                raise ConnectionError("no hay conexión con el pool")
            self._sock.sendall(data)

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
            if not slot[0].wait(timeout):
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
            raise StratumError(f"respuesta inesperada a mining.subscribe: {result!r}")
        self._set_extranonce(result[1], result[2])
        authorized = self.call("mining.authorize", [username, password])
        if authorized is not True:
            raise StratumError("el pool rechazó el usuario (¿dirección de wallet no válida para este pool?)")
        if suggest_difficulty:
            # Algunos pools (ckpool) solo aceptan enteros aquí.
            value = int(suggest_difficulty) if float(suggest_difficulty).is_integer() else suggest_difficulty
            self.request("mining.suggest_difficulty", [value])

    def submit(self, username: str, job_id: str, extranonce2: str, ntime: str, nonce: str) -> int:
        return self.request("mining.submit", [username, job_id, extranonce2, ntime, nonce])

    def _set_extranonce(self, extranonce1, extranonce2_size) -> None:
        if not isinstance(extranonce1, str) or not isinstance(extranonce2_size, int):
            raise StratumError("extranonce no válido")
        if not 1 <= extranonce2_size <= 32:
            raise StratumError(f"tamaño de extranonce2 no soportado: {extranonce2_size}")
        self.extranonce1 = bytes.fromhex(extranonce1)
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
                    if line:
                        self._handle_line(line)
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
            self.events.put(("message", f"línea no válida del pool: {line[:200]!r}"))
            return
        if not isinstance(message, dict):
            return
        method = message.get("method")
        params = message.get("params") or []
        if method:
            self._handle_method(message.get("id"), method, params)
            return
        request_id = message.get("id")
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
            self.events.put(("message", str(params[0]) if params else ""))
        elif method == "client.get_version":
            self._reply(request_id, AGENT)
        elif method == "mining.ping":
            self._reply(request_id, "pong")
        elif request_id is not None:
            self._reply(request_id, None, [20, f"método no soportado: {method}", None])

    def _reply(self, request_id, result, error=None) -> None:
        if request_id is None:
            return
        try:
            self._send({"id": request_id, "result": result, "error": error})
        except OSError:
            pass


def format_error(error) -> str:
    if isinstance(error, list) and len(error) >= 2:
        return f"{error[1]} (código {error[0]})"
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)
