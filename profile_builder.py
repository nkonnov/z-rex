import trex_setup  # noqa: F401  — настраивает sys.path до импорта trex.*
from trex.astf.api import (
    ASTFAssociationRule,
    ASTFIPGen,
    ASTFIPGenDist,
    ASTFIPGenGlobal,
    ASTFProfile,
    ASTFProgram,
    ASTFTCPClientTemplate,
    ASTFTCPServerTemplate,
    ASTFTemplate,
)


class TrafficProfileBuilder:
    """Построитель ASTF-профиля: HTTP + HTTPS (TLS) + DNS + SQLi-атака."""

    def __init__(self, client_ip_range=("16.0.0.1", "16.0.0.254"),
                 server_ip_range=("48.0.0.1", "48.0.0.254")):
        self.client_ip_range = list(client_ip_range)
        self.server_ip_range = list(server_ip_range)

    def _create_ip_gen(self):
        ip_gen_c = ASTFIPGenDist(ip_range=self.client_ip_range, distribution="seq")
        ip_gen_s = ASTFIPGenDist(ip_range=self.server_ip_range, distribution="seq")
        return ASTFIPGen(glob=ASTFIPGenGlobal(ip_offset="1.0.0.0"),
                         dist_client=ip_gen_c, dist_server=ip_gen_s)

    def _create_http_template(self, ip_gen, cps=70, payload_size_kb=10):
        http_req = (
            "GET /index.html HTTP/1.1\r\n"
            "Host: test-app.local\r\n"
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            "Connection: keep-alive\r\n\r\n"
        )
        body = "A" * (payload_size_kb * 1024)
        http_resp = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: text/html\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
            f"{body}"
        )

        prog_c = ASTFProgram()
        prog_c.send(http_req)
        prog_c.recv(len(http_resp))

        prog_s = ASTFProgram()
        prog_s.recv(len(http_req))
        prog_s.send(http_resp)

        temp_c = ASTFTCPClientTemplate(program=prog_c, ip_gen=ip_gen, cps=cps, port=80)
        temp_s = ASTFTCPServerTemplate(program=prog_s, assoc=ASTFAssociationRule(port=80))
        return ASTFTemplate(client_template=temp_c, server_template=temp_s)

    def _create_https_template(self, ip_gen, cps=40, payload_size_kb=15):
        """Эмуляция TLS 1.2/1.3 сессии (ClientHello -> ServerHello/Cert -> Encrypted AppData)."""
        tls_client_hello = (
            b"\x16\x03\x01\x00\x55\x01\x00\x00\x51\x03\x03"
            + b"\x11\x22\x33\x44" * 8
            + b"\x00\x00\x04\xc0\x2f\xc0\x30\x01\x00\x00\x24"
            + b"\x00\x00\x00\x18\x00\x16\x00\x00\x13secure-portal.local"
            + b"\x00\x0a\x00\x04\x00\x02\x00\x17"
        )
        tls_server_hello = (
            b"\x16\x03\x03\x00\x4a\x02\x00\x00\x46\x03\x03"
            + b"\xaa\xbb\xcc\xdd" * 8
            + b"\x00\xc0\x2f\x00\x00\x1e\x0b\x00\x00\x1a\x00\x00\x17"
            + b"FAKE_CERT_PAYLOAD_DATA_"
            + b"\x16\x03\x03\x00\x04\x0e\x00\x00\x00"
        )
        tls_client_finished = (
            b"\x16\x03\x03\x00\x25\x10\x00\x00\x21\x20" + (b"\x55" * 32)
            + b"\x14\x03\x03\x00\x01\x01"
            + b"\x16\x03\x03\x00\x28" + (b"\x99" * 40)
        )
        tls_app_req = b"\x17\x03\x03\x01\x00" + (b"\xef" * 256)
        enc_body = b"\xab" * (payload_size_kb * 1024)
        # TLS record length — 2 байта (макс. 16384 на запись): режем тело на записи,
        # иначе payload > 64 КБ даёт OverflowError при сборке профиля.
        MAX_TLS_RECORD = 16384
        records = []
        for off in range(0, len(enc_body), MAX_TLS_RECORD):
            chunk = enc_body[off:off + MAX_TLS_RECORD]
            records.append(b"\x17\x03\x03" + len(chunk).to_bytes(2, byteorder="big") + chunk)
        tls_app_resp = b"".join(records)

        prog_c = ASTFProgram()
        prog_c.send(tls_client_hello)
        prog_c.recv(len(tls_server_hello))
        prog_c.send(tls_client_finished + tls_app_req)
        prog_c.recv(len(tls_app_resp))

        prog_s = ASTFProgram()
        prog_s.recv(len(tls_client_hello))
        prog_s.send(tls_server_hello)
        prog_s.recv(len(tls_client_finished + tls_app_req))
        prog_s.send(tls_app_resp)

        temp_c = ASTFTCPClientTemplate(program=prog_c, ip_gen=ip_gen, cps=cps, port=443)
        temp_s = ASTFTCPServerTemplate(program=prog_s, assoc=ASTFAssociationRule(port=443))
        return ASTFTemplate(client_template=temp_c, server_template=temp_s)

    def _create_sqli_attack_template(self, ip_gen, cps=10):
        attack_req = (
            "GET /login.php?user=admin'%20OR%201=1--&pass=1234 HTTP/1.1\r\n"
            "Host: vulnerable-app.local\r\n"
            "X-Forwarded-For: ../../../../etc/passwd\r\n"
            "User-Agent: sqlmap/1.7\r\n\r\n"
        )
        blocked_or_ok_resp = "HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\nAccess Granted!"

        prog_c = ASTFProgram()
        prog_c.send(attack_req)
        prog_c.recv(len(blocked_or_ok_resp), clear=True)

        prog_s = ASTFProgram()
        prog_s.recv(len(attack_req))
        prog_s.send(blocked_or_ok_resp)

        temp_c = ASTFTCPClientTemplate(program=prog_c, ip_gen=ip_gen, cps=cps, port=8080)
        temp_s = ASTFTCPServerTemplate(program=prog_s, assoc=ASTFAssociationRule(port=8080))
        return ASTFTemplate(client_template=temp_c, server_template=temp_s)

    def _create_dns_udp_template(self, ip_gen, cps=20):
        dns_query = b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01'
        dns_reply = b'\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22'

        prog_c = ASTFProgram(stream=False)
        prog_c.send_msg(dns_query)
        prog_c.recv_msg(1)

        prog_s = ASTFProgram(stream=False)
        prog_s.recv_msg(1)
        prog_s.send_msg(dns_reply)

        temp_c = ASTFTCPClientTemplate(program=prog_c, ip_gen=ip_gen, cps=cps, port=53)
        temp_s = ASTFTCPServerTemplate(program=prog_s, assoc=ASTFAssociationRule(port=53))
        return ASTFTemplate(client_template=temp_c, server_template=temp_s)

    def build_mix(self, http_cps=50, https_cps=30, dns_cps=15, sqli_cps=5, http_payload_kb=15):
        """Собрать профиль из смеси трафика. Все значения — CPS (соединений/с)."""
        ip_gen = self._create_ip_gen()
        templates = []
        if http_cps > 0:
            templates.append(self._create_http_template(ip_gen, cps=http_cps, payload_size_kb=http_payload_kb))
        if https_cps > 0:
            templates.append(self._create_https_template(ip_gen, cps=https_cps, payload_size_kb=http_payload_kb))
        if dns_cps > 0:
            templates.append(self._create_dns_udp_template(ip_gen, cps=dns_cps))
        if sqli_cps > 0:
            templates.append(self._create_sqli_attack_template(ip_gen, cps=sqli_cps))

        if not templates:
            raise ValueError("Хотя бы один тип трафика должен иметь CPS больше 0")

        return ASTFProfile(default_ip_gen=ip_gen, templates=templates)
