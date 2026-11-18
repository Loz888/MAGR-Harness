"""Trusted standalone DNS/route controller, copied into the offline fixture image.

It runs only inside a namespace with no NIC or default route. DNS answers a
fixed allowlist, never forwards, and the application container receives none
of this process's initial setup capabilities or certificate private keys.
"""

from __future__ import annotations

import os
import select
import socket
import struct
import subprocess
import time

HOSTS = {"localhost": "127.0.0.1", "example.com": "127.0.0.2", "www.google.com": "127.0.0.3"}


def dns_answer(packet: bytes) -> bytes:
    if len(packet) < 12 or len(packet) > 512:
        return b""
    identity, flags, questions, _, _, _ = struct.unpack("!6H", packet[:12])
    if questions != 1 or flags & 0xF800:
        return b""
    cursor, labels = 12, []
    try:
        while True:
            size = packet[cursor]
            cursor += 1
            if size == 0:
                break
            if size > 63 or cursor + size > len(packet):
                return b""
            labels.append(packet[cursor:cursor + size].decode("ascii").lower())
            cursor += size
        query_type, query_class = struct.unpack("!HH", packet[cursor:cursor + 4])
    except (IndexError, UnicodeError, struct.error):
        return b""
    end = cursor + 4
    host = ".".join(labels)
    known = host in HOSTS and query_class == 1
    answer = b""
    if known and query_type == 1:
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(HOSTS[host])
    # AAAA of a known IPv4-only endpoint is NOERROR/NODATA, not NXDOMAIN.
    response_flags = 0x8480 | (flags & 0x0100) | (0 if known else 3)
    return struct.pack("!6H", identity, response_flags, 1, int(bool(answer)), 0, 0) + (
        packet[12:end] + answer
    )


def timeout_routes() -> None:
    netlink = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    netlink.bind((0, 0))
    for sequence, address in enumerate(("10.255.255.1", "123.123.123.123"), 1):
        def attribute(kind: int, data: bytes) -> bytes:
            size = 4 + len(data)
            return struct.pack("HH", size, kind) + data + b"\0" * ((-size) % 4)
        payload = struct.pack("BBBBBBBBI", socket.AF_INET, 32, 0, 0, 254, 3, 253, 1, 0)
        payload += attribute(1, socket.inet_aton(address)) + attribute(4, struct.pack("I", 1))
        netlink.send(struct.pack("IHHII", 16 + len(payload), 24, 1 | 4 | 0x400 | 0x200,
                                 sequence, 0) + payload)
        reply = netlink.recv(4096)
        if len(reply) < 20 or struct.unpack_from("i", reply, 16)[0] != 0:
            raise RuntimeError("Offline timeout route setup failed")
    netlink.close()


def addrconfig_interface() -> None:
    """Advertise usable IPv4 without a peer, external link or default route.

    ADDRCONFIG consumers intentionally ignore loopback. An unconnected dummy
    with one local /32 represents IPv4 availability without granting egress.
    """
    def attribute(kind: int, data: bytes) -> bytes:
        size = 4 + len(data)
        return struct.pack("HH", size, kind) + data + b"\0" * ((-size) % 4)

    netlink = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    netlink.bind((0, 0))

    def apply(kind: int, payload: bytes, sequence: int) -> None:
        netlink.send(struct.pack("IHHII", 16 + len(payload), kind, 1 | 4 | 0x400 | 0x200,
                                 sequence, 0) + payload)
        reply = netlink.recv(4096)
        if len(reply) < 20 or struct.unpack_from("i", reply, 16)[0] != 0:
            raise RuntimeError("Offline ADDRCONFIG interface setup failed")

    info = struct.pack("BBHiII", socket.AF_UNSPEC, 0, 0, 0, 1, 1)
    info += attribute(3, b"harness0\0")
    info += attribute(18 | 0x8000, attribute(1, b"dummy\0"))
    apply(16, info, 100)  # RTM_NEWLINK, IFLA_LINKINFO(kind=dummy), UP.
    interface = socket.if_nametoindex("harness0")
    address = socket.inet_aton("192.0.2.2")
    info = struct.pack("BBBBI", socket.AF_INET, 32, 0x80, 0, interface)
    info += attribute(1, address) + attribute(2, address)
    apply(20, info, 101)  # RTM_NEWADDR, exactly one local /32.
    netlink.close()


def main() -> None:
    addrconfig_interface()
    timeout_routes()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 53))
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind(("127.0.0.1", 53))
    tcp.listen(8)
    child = subprocess.Popen(
        ["node", "/opt/harness/offline_http2.cjs"], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env={"PATH": os.environ["PATH"], "UV_THREADPOOL_SIZE": "1"},
    )
    assert child.stdout is not None
    readable, _, _ = select.select([child.stdout], [], [], 10)
    if not readable or child.stdout.readline(256) != b"HARNESS_OFFLINE_TLS_READY\n":
        raise RuntimeError("Offline TLS service did not become ready")
    os.setgroups([])
    os.setgid(65532)
    os.setuid(65532)  # Linux drops all permitted/effective setup capabilities.
    with open("/proc/self/status") as status:
        if any(line.startswith("CapEff:") and int(line.split()[1], 16) for line in status):
            raise RuntimeError("Setup capabilities were not dropped")
    print("HARNESS_OFFLINE_NETWORK_READY", flush=True)
    peers: dict[socket.socket, tuple[bytearray, float]] = {}
    while child.poll() is None:
        ready_sockets, _, _ = select.select([udp, tcp, *peers], [], [], .1)
        for stream in ready_sockets:
            if stream is udp:
                packet, address = udp.recvfrom(513)
                answer = dns_answer(packet)
                if answer:
                    udp.sendto(answer, address)
            elif stream is tcp:
                peer, _ = tcp.accept()
                peer.setblocking(False)
                if len(peers) >= 16:
                    peer.close()
                else:
                    peers[peer] = (bytearray(), time.monotonic())
            else:
                try:
                    chunk = stream.recv(1024)
                    data, _ = peers[stream]
                    data.extend(chunk)
                    if not chunk or len(data) > 514:
                        raise ValueError("DNS query exceeded bound")
                    if len(data) >= 2 and len(data) >= 2 + struct.unpack("!H", data[:2])[0]:
                        length = struct.unpack("!H", data[:2])[0]
                        answer = dns_answer(bytes(data[2:2 + length]))
                        if answer:
                            stream.sendall(struct.pack("!H", len(answer)) + answer)
                        stream.close()
                        del peers[stream]
                except (OSError, ValueError):
                    stream.close()
                    peers.pop(stream, None)
        for peer, (_, started) in tuple(peers.items()):
            if time.monotonic() - started > 1:
                peer.close()
                del peers[peer]
    raise RuntimeError("Offline TLS service exited")


if __name__ == "__main__":
    main()
