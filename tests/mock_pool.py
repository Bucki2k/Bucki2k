"""Pool Stratum falso para los tests (escucha solo en 127.0.0.1).

Verifica cada share con su propio código, escrito de forma independiente al
del minero, para que un error de orden de bytes no pase desapercibido.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading


def dsha(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def make_coinbase_parts(script_pubkey: bytes, value_sats: int, extranonce_len: int,
                        fee_script: bytes | None = None, fee_sats: int = 0) -> tuple[str, str]:
    """coinb1/coinb2 de una coinbase que paga ``value_sats`` a ``script_pubkey``."""
    height_push = bytes([3]) + (900_000).to_bytes(3, "little")
    script_sig_len = len(height_push) + extranonce_len
    coinb1 = (bytes.fromhex("01000000") + b"\x01" + bytes(32) + b"\xff\xff\xff\xff"
              + bytes([script_sig_len]) + height_push)
    outputs = [(value_sats, script_pubkey)]
    if fee_script is not None:
        outputs.append((fee_sats, fee_script))
    coinb2 = b"\xff\xff\xff\xff" + bytes([len(outputs)])
    for value, script in outputs:
        coinb2 += value.to_bytes(8, "little") + bytes([len(script)]) + script
    coinb2 += bytes(4)
    return coinb1.hex(), coinb2.hex()


class MockPool:
    def __init__(self, *, coinb1: str, coinb2: str, difficulty: float, nbits: str = "1d00ffff",
                 extranonce1: str = "f000000a", extranonce2_size: int = 4,
                 authorize_result=True, disconnect_after_shares: int | None = None,
                 raw_after_authorize: tuple = (), followup_coinb2: str | None = None,
                 followup_delay: float = 1.0):
        self.coinb1 = coinb1
        self.coinb2 = coinb2
        # Líneas en bruto (posiblemente malformadas) que se envían tras autorizar.
        self.raw_after_authorize = raw_after_authorize
        # Segundo trabajo (job2) con otra coinbase, enviado al cabo de un rato.
        self.jobs = {"job1": coinb2}
        if followup_coinb2 is not None:
            self.jobs["job2"] = followup_coinb2
        self.followup_delay = followup_delay
        self.blocks: list[dict] = []
        self._send_lock = threading.Lock()
        self.difficulty = difficulty
        self.nbits = nbits
        self.extranonce1 = extranonce1
        self.extranonce2_size = extranonce2_size
        self.authorize_result = authorize_result
        self.disconnect_after_shares = disconnect_after_shares
        self.branches = [dsha(bytes([i])).hex() for i in range(3)]
        self.prevhash = dsha(b"prev").hex()
        self.version = "20000000"
        self.ntime = "66f00000"
        self.submissions: list[dict] = []
        self._seen_shares: set = set()
        self.requests: list[dict] = []
        self.replies: list[dict] = []
        self.connections = 0
        self.lock = threading.Lock()
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._stopped = threading.Event()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        self._stopped.set()
        self._server.close()

    def valid_shares(self) -> int:
        with self.lock:
            return sum(1 for s in self.submissions if s["valid"])

    # --- servidor ------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _send(self, conn, message) -> None:
        data = message if isinstance(message, str) else json.dumps(message)
        with self._send_lock:
            conn.sendall((data + "\n").encode())

    def _notify(self, conn, job_id: str, clean: bool) -> None:
        try:
            self._send(conn, {"id": None, "method": "mining.notify", "params": [
                job_id, self.prevhash, self.coinb1, self.jobs[job_id], self.branches,
                self.version, self.nbits, self.ntime, clean]})
        except OSError:
            pass

    def _serve(self, conn: socket.socket) -> None:
        shares_this_connection = 0
        with conn, conn.makefile("r", encoding="utf-8") as reader:
            for line in reader:
                message = json.loads(line)
                with self.lock:
                    if "method" in message:
                        self.requests.append(message)
                    else:
                        self.replies.append(message)
                method = message.get("method")
                request_id = message.get("id")
                if method == "mining.subscribe":
                    self._send(conn, {"id": request_id, "error": None, "result": [
                        [["mining.set_difficulty", "1"], ["mining.notify", "1"]],
                        self.extranonce1, self.extranonce2_size]})
                elif method == "mining.authorize":
                    self._send(conn, {"id": request_id, "result": self.authorize_result, "error": None})
                    if self.authorize_result is True:
                        for raw in self.raw_after_authorize:
                            self._send(conn, raw)
                        self._send(conn, {"id": 9001, "method": "mining.ping", "params": []})
                        self._send(conn, {"id": None, "method": "mining.set_difficulty",
                                          "params": [self.difficulty]})
                        self._notify(conn, "job1", True)
                        if "job2" in self.jobs:
                            threading.Timer(self.followup_delay, self._notify,
                                            args=(conn, "job2", False)).start()
                elif method == "mining.suggest_difficulty":
                    self._send(conn, {"id": request_id, "result": True, "error": None})
                elif method == "mining.submit":
                    valid, reason = self._check_share(message["params"])
                    with self.lock:
                        self.submissions.append({"params": message["params"], "valid": valid,
                                                 "reason": reason})
                    self._send(conn, {"id": request_id, "result": valid,
                                      "error": None if valid else [23, reason, None]})
                    shares_this_connection += 1
                    if (self.disconnect_after_shares is not None
                            and shares_this_connection >= self.disconnect_after_shares):
                        conn.shutdown(socket.SHUT_RDWR)
                        return

    def _check_share(self, params) -> tuple[bool, str]:
        _user, job_id, extranonce2, ntime, nonce = params
        if job_id not in self.jobs:
            return False, "trabajo desconocido"
        if len(extranonce2) != 2 * self.extranonce2_size or len(nonce) != 8 or len(ntime) != 8:
            return False, "longitud incorrecta"
        if (extranonce2, ntime, nonce) in self._seen_shares:
            return False, "share duplicada"
        self._seen_shares.add((extranonce2, ntime, nonce))
        coinbase = bytes.fromhex(self.coinb1 + self.extranonce1 + extranonce2 + self.jobs[job_id])
        root = dsha(coinbase)
        for branch in self.branches:
            root = dsha(root + bytes.fromhex(branch))
        prev = bytes.fromhex(self.prevhash)
        prev = b"".join(prev[i:i + 4][::-1] for i in range(0, 32, 4))
        header = (bytes.fromhex(self.version)[::-1] + prev + root + bytes.fromhex(ntime)[::-1]
                  + bytes.fromhex(self.nbits)[::-1] + bytes.fromhex(nonce)[::-1])
        value = int.from_bytes(dsha(header), "little")
        target = int(0x00000000FFFF0000000000000000000000000000000000000000000000000000 / self.difficulty)
        bits = int(self.nbits, 16)
        network_target = (bits & 0x007FFFFF) << (8 * ((bits >> 24) - 3))
        if value <= network_target:
            # Como ckpool: un bloque vale aunque no llegue a la dificultad de share.
            with self.lock:
                self.blocks.append({"job_id": job_id, "nonce": nonce})
            return True, ""
        if value > target:
            return False, "dificultad baja"
        return True, ""
