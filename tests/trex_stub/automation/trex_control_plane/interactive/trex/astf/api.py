"""Заглушка TRex ASTF API для smoke-теста без реального генератора."""
import time


class _Synthetic:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class ASTFAssociationRule(_Synthetic): pass
class ASTFIPGen(_Synthetic): pass
class ASTFIPGenDist(_Synthetic): pass
class ASTFIPGenGlobal(_Synthetic): pass
class ASTFProfile(_Synthetic): pass
class ASTFTCPClientTemplate(_Synthetic): pass
class ASTFTCPServerTemplate(_Synthetic): pass
class ASTFTemplate(_Synthetic): pass


class ASTFProgram:
    def __init__(self, stream=True):
        self.stream = stream
    def send(self, msg, **kw): pass
    def recv(self, n, **kw): pass
    def send_msg(self, msg): pass
    def recv_msg(self, n): pass


class ASTFClient:
    def __init__(self, server="127.0.0.1"):
        self.server = server
        self._traffic = False
        self._mult = 0.0
        self._t0 = 0.0
        self._end = 0.0
        self._connattempt = 5000

    def connect(self): pass
    def acquire(self, force=False): pass
    def disconnect(self): pass

    def is_traffic_active(self):
        if self._traffic and time.time() >= self._end:  # TRex сам глушит по duration
            self._traffic = False
        return self._traffic

    def get_port_info(self):
        return [
            {"index": 0, "link": {"up": True}, "speed": 10},
            {"index": 1, "link": {"up": True}, "speed": 10},
        ]

    def reset(self): self._traffic = False
    def stop(self): self._traffic = False
    def load_profile(self, p): pass

    def start(self, mult=1.0, duration=60, **kw):
        self._traffic = True
        self._mult = mult
        self._t0 = time.time()
        self._end = self._t0 + duration

    def update(self, mult=None, **kw):
        if mult is not None:
            self._mult = mult

    def get_stats(self):
        if not self.is_traffic_active():
            return {
                "global": {"tx_bps": 0, "rx_bps": 0, "tx_pps": 0, "rx_pps": 0},
                "traffic": {"client": {
                    "m_active_flows": 0, "m_est_flows": 0, "m_tx_bw_l7_r": 0,
                    "m_rx_bw_l7_r": 0, "tcps_connattempt": self._connattempt,
                    "tcps_closed": 4900, "tcps_conndrops": 5,
                    "tcps_rexmttimeo_syn": 2}},
                "0": {"ibytes": 1000, "obytes": 1000},
                "1": {"ibytes": 1000, "obytes": 1000},
            }
        elapsed = max(0.0, time.time() - self._t0)
        rate = self._mult * 1e9  # 1 mult ~ 1 Гбит/с
        ob = int(rate * elapsed / 8)
        self._connattempt = 5000 + int(elapsed * 100)
        return {
            "global": {"tx_bps": rate, "rx_bps": rate * 0.95,
                         "tx_pps": rate / 600, "rx_pps": rate * 0.95 / 600},
            "traffic": {"client": {
                "m_active_flows": 1000, "m_est_flows": 900,
                "m_tx_bw_l7_r": rate * 0.8, "m_rx_bw_l7_r": rate * 0.76,
                "tcps_connattempt": self._connattempt,
                "tcps_closed": 4900, "tcps_conndrops": 5,
                "tcps_rexmttimeo_syn": 2}},
            "0": {"ibytes": ob, "obytes": ob},
            "1": {"ibytes": ob, "obytes": ob},
        }
