"""Read-only connectivity check for IB Gateway (paper trading).

Uses only the Python standard library: performs the TWS API handshake,
prints the server version and managed account(s), then disconnects.
It never places, modifies, or cancels orders.
"""
import socket
import struct
import sys

HOST = "127.0.0.1"
PORT = 4002  # IB Gateway paper port (live is 4001)
CLIENT_ID = 99


def send_msg(sock, payload: bytes):
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def recv_msg(sock) -> list[str]:
    header = _recv_exact(sock, 4)
    (size,) = struct.unpack(">I", header)
    body = _recv_exact(sock, size)
    return body.decode(errors="replace").split("\0")[:-1]


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Gateway closed the connection")
        buf += chunk
    return buf


def main():
    sock = socket.create_connection((HOST, PORT), timeout=10)
    # Handshake: "API\0" + length-prefixed supported version range
    sock.sendall(b"API\0" + struct.pack(">I", len(b"v100..187")) + b"v100..187")
    server_version, conn_time = recv_msg(sock)[:2]
    print(f"Connected. Server version {server_version}, time {conn_time}")

    # startApi: msgId 71, version 2, clientId, optional capabilities
    send_msg(sock, f"71\x002\x00{CLIENT_ID}\x00\x00".encode())

    accounts = None
    try:
        while accounts is None:
            fields = recv_msg(sock)
            if fields[0] == "15":  # MANAGED_ACCTS
                accounts = [a for a in fields[2].split(",") if a]
            elif fields[0] == "4":  # ERR_MSG (farm status notices etc.)
                print("  gateway msg:", " | ".join(fields[2:5]))
    finally:
        sock.close()

    print("Managed accounts:", accounts)
    if all(a.startswith("D") for a in accounts):
        print("OK: paper account(s) detected (paper IDs start with 'D').")
    else:
        print("WARNING: a non-paper account ID was returned. Stop and check Gateway login.")
        sys.exit(1)


if __name__ == "__main__":
    main()
