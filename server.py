"""NetStrike L4-L7 Benchmark API (FastAPI + Cisco TRex ASTF).

Переменные окружения:
  NETSTRIKE_API_TOKEN    если задан — все /api/* и WebSocket требуют токен
                         (заголовок X-API-Key, Authorization: Bearer, или ?token= для WS)
  NETSTRIKE_REPORTS_DIR  папка отчётов (по умолчанию ./reports рядом с server.py)
  TREX_PATH              путь к TRex (см. trex_setup.py)

Запуск (слушать только localhost, доступ наружу — через reverse proxy):
  uvicorn server:app --host 127.0.0.1 --port 8000
"""
import asyncio
import contextlib
import os
import secrets
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import trex_setup  # noqa: F401  настраивает sys.path; должен быть раньше trex.*
from trex.astf.api import ASTFClient
from profile_builder import TrafficProfileBuilder
from report_generator import ReportManager


# ---------------------------------------------------------------------------
# Конфигурация и константы методики
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.environ.get("NETSTRIKE_REPORTS_DIR", os.path.join(BASE_DIR, "reports"))
API_TOKEN = os.environ.get("NETSTRIKE_API_TOKEN", "").strip()

PROFILE_CUSTOM = "custom_mix"
PROFILE_EMIX2 = "astf_emix2"
EMIX2_DURATION_S = 86400          # «бесконечный» прогон, останавливается явно
SAMPLE_INTERVAL_S = 0.5           # шаг опроса счётчиков внутри окна измерения
CONNECT_BACKOFF_S = 3.0           # пауза между попытками подключения к TRex
PORT_INFO_TTL_S = 5.0             # кэш состояния портов
MIN_RATE_GBPS = 0.1               # ниже этого полосу считаем шумом
BW_DEGRADATION_RATIO = 0.92       # падение RX относительно пика успешных шагов
MIN_PEAK_FOR_DEGRADATION = 0.5    # Гбит/с, ниже — деградацию не проверяем

trex_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trex")
report_mgr = ReportManager(reports_dir=REPORTS_DIR)
report_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Модели запросов
# ---------------------------------------------------------------------------
class TestConfig(BaseModel):
    # *_weight — это CPS (соединений в секунду), а не весовые коэффициенты.
    profile_mode: Literal["custom_mix", "astf_emix2"] = Field("custom_mix")
    http_weight: int = Field(50, ge=0, le=500)
    https_weight: int = Field(30, ge=0, le=500)
    dns_weight: int = Field(15, ge=0, le=500)
    sqli_weight: int = Field(5, ge=0, le=500)
    http_payload_kb: int = Field(15, ge=1, le=1024)
    target_gbps: float = Field(2.0, gt=0, le=100.0)
    duration: int = Field(60, ge=1, le=86400)


class UpdateGbpsRequest(BaseModel):
    target_gbps: float = Field(..., gt=0, le=100.0)


class AutoBenchConfig(BaseModel):
    start_gbps: float = Field(2.0, gt=0, le=100.0)
    step_gbps: float = Field(2.0, gt=0, le=50.0)
    max_gbps: float = Field(10.0, gt=0, le=100.0)
    step_duration: int = Field(5, ge=4, le=60)
    max_drop_pct: float = Field(2.0, ge=0.0, le=100.0)
    enable_binary: bool = Field(True)
    binary_precision_gbps: float = Field(0.2, gt=0)
    # Новые параметры методики (значения по умолчанию совместимы с текущим UI):
    bw_tolerance_pct: float = Field(3.0, ge=0.0, le=50.0)        # допуск RX < TX
    baseline_max_drop_pct: float = Field(5.0, ge=0.0, le=100.0)  # абсолютный потолок потерь на старте
    confirm_failures: bool = Field(True)                          # повторять замер перед фиксацией провала


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------
def _token_ok(supplied: str) -> bool:
    if not API_TOKEN:
        return True
    return secrets.compare_digest(supplied.encode("utf-8"), API_TOKEN.encode("utf-8"))


def require_token(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> None:
    supplied = x_api_key or ""
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:]
    if not _token_ok(supplied):
        raise HTTPException(status_code=401, detail="Нужен API-токен (заголовок X-API-Key)")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _fresh_autobench_state() -> Dict[str, Any]:
    return {
        "active": False,
        "phase": "idle",
        "status": "idle",
        "current_gbps": 0.0,
        "low_gbps": 0.0,
        "high_gbps": 0.0,
        "best_gbps": 0.0,
        "best_rx_gbps": 0.0,
        "best_mpps": 0.0,
        "message": "Готов к поиску",
    }


def _port_is_up(p: dict) -> bool:
    link = p.get("link", "")
    if isinstance(link, dict):
        return bool(link.get("up", False))
    if isinstance(link, bool):
        return link
    return str(link).upper() == "UP"


def _port_bytes(stats: dict) -> Tuple[int, int]:
    """Суммарные ibytes/obytes по числовым ключам портов TRex (int или str)."""
    ibytes = obytes = 0
    for key, val in stats.items():
        if str(key).isdigit() and isinstance(val, dict):
            ibytes += int(val.get("ibytes", 0))
            obytes += int(val.get("obytes", 0))
    return ibytes, obytes


def _estimate_base_gbps(config: TestConfig) -> float:
    """Оценка полосы custom_mix при mult=1. Грубая: константы — приближение заголовков."""
    http_b = config.http_weight * (config.http_payload_kb * 1024 + 450)
    https_b = config.https_weight * (config.http_payload_kb * 1024 + 750)
    dns_b = config.dns_weight * 160
    sqli_b = config.sqli_weight * 400
    return max(0.001, (http_b + https_b + dns_b + sqli_b) * 8 / 1e9)


@dataclass
class StepResult:
    applied_mult: float
    tx_gbps: float
    rx_gbps: float
    tx_mpps: float
    drop_pct: float
    drops_delta: int


# ---------------------------------------------------------------------------
# Управление TRex
# ---------------------------------------------------------------------------
class TRexManager:
    """Владелец соединения с TRex.

    Все вызовы к TRex выполняются в одном рабочем потоке (trex_executor),
    поэтому внутренние структуры не требуют отдельных блокировок. Лок нужен
    только для состояния автопоиска, которое читается из других потоков.
    """

    def __init__(self, server_ip: str = "127.0.0.1"):
        self.server_ip = server_ip
        self.client: Optional[ASTFClient] = None
        self.builder = TrafficProfileBuilder()
        # Коэффициент «mult на 1 Гбит/с» отдельно для каждого профиля.
        self._mult_per_gbps: Dict[str, float] = {}
        self._active_profile: Optional[str] = None
        self._last_connect_fail = 0.0
        self._idle_ticks = 0
        self._port_cache: list = []
        self._port_cache_ts = 0.0
        self._state_lock = threading.Lock()
        self._autobench_state = _fresh_autobench_state()
        self.autobench_task: Optional[asyncio.Task] = None

    # --- состояние автопоиска (потокобезопасно) ---------------------------
    def ab_reset(self) -> None:
        with self._state_lock:
            self._autobench_state = _fresh_autobench_state()

    def ab_update(self, **kwargs) -> None:
        with self._state_lock:
            self._autobench_state.update(kwargs)

    def ab_snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._autobench_state)

    def ab_active(self) -> bool:
        with self._state_lock:
            return bool(self._autobench_state["active"])

    def autobench_summary(self) -> dict:
        s = self.ab_snapshot()
        if s["best_gbps"] <= 0:
            return {}
        return {
            "autobench_target_gbps": s["best_gbps"],
            "autobench_actual_rx_gbps": s["best_rx_gbps"],
            "autobench_best_mpps": s["best_mpps"],
            "autobench_result": s["message"],
        }

    # --- соединение --------------------------------------------------------
    def connect(self) -> None:
        if self.client is not None:
            return
        if time.monotonic() - self._last_connect_fail < CONNECT_BACKOFF_S:
            raise ConnectionError("TRex недоступен, повторная попытка через несколько секунд")
        c = ASTFClient(server=self.server_ip)
        try:
            c.connect()
            c.acquire(force=True)
        except Exception:
            self._last_connect_fail = time.monotonic()
            with contextlib.suppress(Exception):
                c.disconnect()
            raise
        self.client = c

    def disconnect(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                self.client.disconnect()
            self.client = None

    def _require_client(self) -> ASTFClient:
        self.connect()
        return self.client

    def check_ports_link(self) -> Dict[str, Any]:
        c = self._require_client()
        ports, down = [], []
        for p in c.get_port_info():
            pid = p.get("index", "?")
            up = _port_is_up(p)
            ports.append({"port": pid, "up": up, "speed_gbps": p.get("speed", 0)})
            if not up:
                down.append(str(pid))
        if down:
            raise RuntimeError(
                f"Физический линк отсутствует (LINK DOWN) на портах: {', '.join(down)}. "
                f"Проверьте трансиверы и патч-корды перед запуском."
            )
        return {"all_up": True, "ports": ports}

    def _ports_status(self, c: ASTFClient) -> list:
        now = time.monotonic()
        if now - self._port_cache_ts > PORT_INFO_TTL_S:
            try:
                self._port_cache = [
                    {"port": p.get("index", 0), "up": _port_is_up(p), "speed": p.get("speed", 0)}
                    for p in c.get_port_info()
                ]
                self._port_cache_ts = now
            except Exception:
                pass  # оставляем предыдущий кэш, повторим на следующем тике
        return self._port_cache

    # --- масштабирование ---------------------------------------------------
    def _mult(self, gbps: float) -> float:
        ratio = self._mult_per_gbps.get(self._active_profile or "")
        if ratio is None:
            raise RuntimeError("Профиль не запущен: коэффициент масштабирования не определён")
        return max(0.01, round(gbps * ratio, 4))

    def calibrate(self, applied_mult: float, measured_tx_gbps: float) -> None:
        """Уточнить коэффициент по фактической полосе (вызывается на первом шаге)."""
        if self._active_profile and applied_mult > 0 and measured_tx_gbps > MIN_RATE_GBPS:
            self._mult_per_gbps[self._active_profile] = applied_mult / measured_tx_gbps

    # --- загрузка и запуск профилей ---------------------------------------
    def _load_emix2(self) -> None:
        path = os.path.join(trex_setup.TREX_PATH, "astf", "emix2.py")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Профиль не найден: {path}")
        prev_cwd = os.getcwd()
        os.chdir(trex_setup.TREX_PATH)  # emix2 резолвит относительные пути к pcap
        try:
            self.client.load_profile(path)
        finally:
            os.chdir(prev_cwd)

    def _custom_loader(self, config: TestConfig) -> Callable[[], None]:
        def load():
            profile = self.builder.build_mix(
                http_cps=config.http_weight,
                https_cps=config.https_weight,
                dns_cps=config.dns_weight,
                sqli_cps=config.sqli_weight,
                http_payload_kb=config.http_payload_kb,
            )
            self.client.load_profile(profile)
        return load

    def _prepare(self) -> None:
        c = self._require_client()
        self.check_ports_link()
        if c.is_traffic_active():
            c.stop()
        c.reset()

    def _start_profile(self, loader: Callable[[], None], profile_key: str,
                       ratio: float, gbps: float, duration: int) -> None:
        for attempt in range(2):
            try:
                self._prepare()
                loader()
                # Коэффициент и профиль выставляем ДО start, чтобы _mult был корректен.
                self._mult_per_gbps[profile_key] = ratio
                self._active_profile = profile_key
                self._idle_ticks = 0
                self.client.start(mult=self._mult(gbps), duration=duration)
                return
            except RuntimeError:
                raise  # ошибки пользователя/линков — без повтора
            except Exception:
                self.disconnect()
                if attempt == 1:
                    raise

    def start_test(self, config: TestConfig) -> None:
        if config.profile_mode == PROFILE_CUSTOM:
            total_cps = config.http_weight + config.https_weight + config.dns_weight + config.sqli_weight
            if total_cps == 0:
                raise RuntimeError("Хотя бы один тип трафика должен иметь CPS больше 0")
            loader = self._custom_loader(config)
            key, ratio = PROFILE_CUSTOM, 1.0 / _estimate_base_gbps(config)
        else:
            loader, key, ratio = self._load_emix2, PROFILE_EMIX2, 1.0  # 1 mult ≈ 1 Гбит/с, уточняется калибровкой

        self._start_profile(loader, key, ratio, config.target_gbps, config.duration)
        # Сессию открываем только после успешного старта — иначе при сбое останется пустой отчёт.
        with report_lock:
            report_mgr.start_session(mode=config.profile_mode, params=config.model_dump())

    def start_emix2_continuous(self, gbps: float) -> None:
        self._start_profile(self._load_emix2, PROFILE_EMIX2, 1.0, gbps, EMIX2_DURATION_S)

    # --- управление и остановка -------------------------------------------
    def update_target_gbps(self, target_gbps: float) -> Optional[float]:
        if self.client is None or not self.client.is_traffic_active():
            return None
        mult = self._mult(target_gbps)
        self.client.update(mult=mult)
        return mult

    def stop_test(self) -> None:
        # connect() перед stop: даже если соединение потеряно, остановим трафик на TRex.
        try:
            c = self._require_client()
            if c.is_traffic_active():
                c.stop()
        except Exception as e:
            print(f"[TRex stop warning]: {e}")

        with report_lock:
            recording = report_mgr.is_recording
            summary = self.autobench_summary() if recording else {}
        if recording:
            report_mgr.finish_session(summary=summary)

    def get_raw_stats(self) -> dict:
        return self._require_client().get_stats()

    # --- телеметрия --------------------------------------------------------
    def get_telemetry(self) -> dict:
        try:
            c = self._require_client()
        except Exception as e:
            return {"status": "disconnected", "ports": [], "detail": str(e),
                    "autobench": self.ab_snapshot()}

        try:
            active = c.is_traffic_active()
            stats = c.get_stats()
            g = stats.get("global", {})
            cs = stats.get("traffic", {}).get("client", {})
            payload = {
                "status": "running" if active else "idle",
                "timestamp": time.strftime("%H:%M:%S"),
                "ports": self._ports_status(c),
                "autobench": self.ab_snapshot(),
                "global": {
                    "tx_gbps": round(g.get("tx_bps", 0) / 1e9, 3),
                    "rx_gbps": round(g.get("rx_bps", 0) / 1e9, 3),
                    "tx_mpps": round(g.get("tx_pps", 0) / 1e6, 3),
                    "rx_mpps": round(g.get("rx_pps", 0) / 1e6, 3),
                },
                "l7_client": {
                    "active_flows": cs.get("m_active_flows", 0),
                    "est_flows": cs.get("m_est_flows", 0),
                    "tx_bw_l7_mbps": round(cs.get("m_tx_bw_l7_r", 0) / 1e6, 2),
                    "rx_bw_l7_mbps": round(cs.get("m_rx_bw_l7_r", 0) / 1e6, 2),
                    "conn_attempts": cs.get("tcps_connattempt", 0),
                    "conn_closed": cs.get("tcps_closed", 0),
                    "conn_drops": cs.get("tcps_conndrops", 0),
                    "syn_timeouts": cs.get("tcps_rexmttimeo_syn", 0),
                },
            }
        except Exception as e:
            self.disconnect()
            return {"status": "disconnected", "ports": [], "detail": str(e),
                    "autobench": self.ab_snapshot()}

        try:
            self._record_tick(payload, active)
        except Exception:
            traceback.print_exc()  # сбой записи отчёта не должен рвать телеметрию
        return payload

    def _record_tick(self, payload: dict, active: bool) -> None:
        finish = False
        with report_lock:
            if active:
                self._idle_ticks = 0
                if report_mgr.is_recording:
                    report_mgr.add_point(payload, self.ab_snapshot() if self.ab_active() else None)
                return
            if self.ab_active() or not report_mgr.is_recording:
                return
            # Завершаем сессию только после двух подряд «idle»-тиков, чтобы
            # кратковременная пауза между запуском и стартом трафика не закрывала отчёт.
            self._idle_ticks += 1
            finish = self._idle_ticks >= 2
        if finish:
            report_mgr.finish_session()


trex_mgr = TRexManager()


async def run_in_trex_thread(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(trex_executor, func, *args)


# ---------------------------------------------------------------------------
# Измерение и методика автопоиска
# ---------------------------------------------------------------------------
async def _apply_and_measure(gbps: float, step_duration: int, settle_s: float,
                             calibrate: bool) -> StepResult:
    applied = await run_in_trex_thread(trex_mgr.update_target_gbps, gbps)
    if applied is None:
        raise RuntimeError("Тест не запущен — нагрузка не применена")
    await asyncio.sleep(settle_s)

    before = await run_in_trex_thread(trex_mgr.get_raw_stats)
    t0 = time.monotonic()
    window_end = t0 + max(2.0, step_duration - 1.5)

    pps_samples = []
    after = before
    while True:
        await asyncio.sleep(SAMPLE_INTERVAL_S)
        after = await run_in_trex_thread(trex_mgr.get_raw_stats)
        pps_samples.append(float(after.get("global", {}).get("tx_pps", 0)))
        if time.monotonic() >= window_end:
            break
    dt = max(0.5, time.monotonic() - t0)

    # Полоса — средняя за окно по аппаратным счётчикам портов (а не мгновенное значение).
    ib0, ob0 = _port_bytes(before)
    ib1, ob1 = _port_bytes(after)
    if ob1 > ob0 and ib1 > ib0:
        tx = (ob1 - ob0) * 8 / dt / 1e9
        rx = (ib1 - ib0) * 8 / dt / 1e9
    else:
        g = after.get("global", {})  # fallback, если счётчики портов недоступны
        tx = g.get("tx_bps", 0) / 1e9
        rx = g.get("rx_bps", 0) / 1e9
    tx, rx = round(tx, 3), round(rx, 3)
    mpps = round(sum(pps_samples) / len(pps_samples) / 1e6, 3)

    cb = before.get("traffic", {}).get("client", {})
    ca = after.get("traffic", {}).get("client", {})
    syn_delta = max(0, ca.get("tcps_rexmttimeo_syn", 0) - cb.get("tcps_rexmttimeo_syn", 0))
    rst_delta = max(0, ca.get("tcps_conndrops", 0) - cb.get("tcps_conndrops", 0))
    attempts = max(1, ca.get("tcps_connattempt", 0) - cb.get("tcps_connattempt", 0))
    drops = syn_delta + rst_delta
    drop_pct = round(drops / attempts * 100.0, 2)

    if calibrate:
        await run_in_trex_thread(trex_mgr.calibrate, applied, tx)

    return StepResult(applied, tx, rx, mpps, drop_pct, drops)


def _verdict(r: StepResult, bw_tolerance_pct: float, drop_limit: float, peak_rx: float) -> Optional[str]:
    """Возвращает причину провала или None, если шаг прошёл."""
    if r.tx_gbps > MIN_RATE_GBPS and r.rx_gbps < r.tx_gbps * (1 - bw_tolerance_pct / 100.0):
        return f"потеря полосы RX {r.rx_gbps}/{r.tx_gbps} Гбит/с"
    if peak_rx > MIN_PEAK_FOR_DEGRADATION and r.rx_gbps < peak_rx * BW_DEGRADATION_RATIO:
        return f"деградация RX: {r.rx_gbps} < пик {peak_rx:.2f} Гбит/с"
    if r.drop_pct > drop_limit:
        return f"потери {r.drop_pct}% (+{r.drops_delta}) > порога {drop_limit:.2f}%"
    return None


async def _run_level(cfg: AutoBenchConfig, gbps: float, settle_s: float, calibrate: bool,
                     drop_limit: float, peak_rx: float) -> Tuple[Optional[str], StepResult]:
    r = await _apply_and_measure(gbps, cfg.step_duration, settle_s, calibrate)
    reason = _verdict(r, cfg.bw_tolerance_pct, drop_limit, peak_rx)
    if reason and cfg.confirm_failures:
        # Провал фиксируем только если повторный замер подтверждает его.
        trex_mgr.ab_update(message=f"Повтор замера {gbps:.2f} Гбит/с ({reason})...")
        r2 = await _apply_and_measure(gbps, cfg.step_duration, 1.5, False)
        reason2 = _verdict(r2, cfg.bw_tolerance_pct, drop_limit, peak_rx)
        if reason2 is None:
            return None, r2
        r, reason = r2, reason2
    return reason, r


async def execute_autobench(cfg: AutoBenchConfig) -> None:
    trex_mgr.ab_reset()
    trex_mgr.ab_update(
        active=True, phase="ramp_up", status="running", current_gbps=cfg.start_gbps,
        message=f"Фаза 1 (Разгон): старт с {cfg.start_gbps} Гбит/с...",
    )

    status = "limit_reached"
    msg = ""
    found_reason = ""
    start_reason = ""
    drop_limit = cfg.baseline_max_drop_pct  # до калибровки — абсолютный потолок
    peak_rx = 0.0
    best_gbps = 0.0
    best_rx = 0.0
    best_mpps = 0.0
    first_bad = 0.0

    try:
        await run_in_trex_thread(trex_mgr.start_emix2_continuous, cfg.start_gbps)
        with report_lock:
            report_mgr.start_session(mode="autobench_emix2", params=cfg.model_dump())

        # Фаза 1: линейный разгон. Уровни считаются от индекса — без накопления ошибки float.
        k = 0
        while True:
            gbps = round(cfg.start_gbps + k * cfg.step_gbps, 3)
            if gbps > cfg.max_gbps + 1e-9:
                break
            is_first = k == 0
            trex_mgr.ab_update(
                phase="ramp_up", current_gbps=gbps, low_gbps=best_gbps, high_gbps=0.0,
                message=f"Фаза 1 (Разгон): проверка {gbps:.2f} Гбит/с (пик RX: {peak_rx:.2f})...",
            )
            reason, r = await _run_level(
                cfg, gbps, settle_s=3.0 if is_first else 1.5,
                calibrate=is_first, drop_limit=drop_limit, peak_rx=peak_rx,
            )
            if reason:
                if is_first:
                    status, start_reason = "start_failed", reason
                else:
                    status, found_reason, first_bad = "found", reason, gbps
                break

            if is_first:
                # Базовый уровень потерь (ниже абсолютного потолка) — допуск над ним.
                drop_limit = r.drop_pct + cfg.max_drop_pct
            best_gbps, best_rx, best_mpps = gbps, r.rx_gbps, r.tx_mpps
            peak_rx = max(peak_rx, r.rx_gbps)
            trex_mgr.ab_update(low_gbps=gbps, best_gbps=gbps, best_rx_gbps=best_rx, best_mpps=best_mpps)
            k += 1

        # Фаза 2: бинарный поиск между последним чистым и первым упавшим уровнем.
        if status == "found" and cfg.enable_binary and best_gbps > 0:
            low, high = best_gbps, first_bad
            while high - low > cfg.binary_precision_gbps + 1e-9:
                mid = round((low + high) / 2.0, 3)
                trex_mgr.ab_update(
                    phase="binary_search", current_gbps=mid, low_gbps=low, high_gbps=high,
                    message=f"Фаза 2 (Бинарный поиск): проверка {mid:.2f} Гбит/с в [{low:.2f} .. {high:.2f}]",
                )
                reason, r = await _run_level(cfg, mid, settle_s=1.5, calibrate=False,
                                             drop_limit=drop_limit, peak_rx=peak_rx)
                if reason:
                    high = mid
                else:
                    low = mid
                    best_gbps, best_rx, best_mpps = mid, r.rx_gbps, r.tx_mpps
                    peak_rx = max(peak_rx, r.rx_gbps)
                    trex_mgr.ab_update(best_gbps=best_gbps, best_rx_gbps=best_rx, best_mpps=best_mpps)
            first_bad = high

        if status == "found":
            msg = (f"Порог найден: {first_bad:.2f} Гбит/с ({found_reason}). Максимум без потерь: "
                   f"{best_gbps:.2f} Гбит/с (RX {best_rx:.2f} Гбит/с / {best_mpps:.2f} Mpps)")
        elif status == "limit_reached":
            msg = (f"Лимит {cfg.max_gbps} Гбит/с достигнут без потерь — предел не найден, "
                   f"увеличьте лимит. Проверено до {best_gbps:.2f} Гбит/с")
        else:
            msg = f"Сбой на старте {cfg.start_gbps} Гбит/с: {start_reason}"

    except asyncio.CancelledError:
        status = "stopped"
        msg = (f"Остановлено вручную. Лучший результат: {best_gbps:.2f} Гбит/с "
               f"(RX {best_rx:.2f} Гбит/с)")
        raise
    except Exception as e:
        traceback.print_exc()
        status = "error"
        msg = f"Ошибка автопоиска: {type(e).__name__}: {e}"
    finally:
        trex_mgr.ab_update(
            active=False, phase="finished", status=status, message=msg,
            best_gbps=best_gbps, best_rx_gbps=best_rx, best_mpps=best_mpps,
        )
        await run_in_trex_thread(trex_mgr.stop_test)


async def _cancel_autobench() -> None:
    """Отменить текущий автопоиск и ДОЖДАТЬСЯ его finally (остановка трафика),
    прежде чем запускать новый тест — иначе finally может остановить новый прогон."""
    task = trex_mgr.autobench_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await run_in_trex_thread(trex_mgr.connect)
    except Exception as e:
        print(f"Ошибка при подключении к TRex: {e}")
    if not API_TOKEN:
        print("[WARN] NETSTRIKE_API_TOKEN не задан: API без авторизации. "
              "Слушайте 127.0.0.1 или задайте токен.", file=sys.stderr)
    yield
    # Корректное завершение: отменить автопоиск, остановить трафик, отключиться.
    await _cancel_autobench()
    await run_in_trex_thread(trex_mgr.stop_test)
    await run_in_trex_thread(trex_mgr.disconnect)


app = FastAPI(title="NetStrike L4-L7 Benchmark API", lifespan=lifespan)
# CORS намеренно не подключён: UI отдаётся с того же origin.


@app.post("/api/test/start", dependencies=[Depends(require_token)])
async def api_start_test(config: TestConfig):
    await _cancel_autobench()
    try:
        await run_in_trex_thread(trex_mgr.start_test, config)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Не удалось запустить тест: {type(e).__name__}: {e}")
    return {"message": f"Тест запущен (целевая полоса: {config.target_gbps} Гбит/с)"}


@app.post("/api/test/autobench", dependencies=[Depends(require_token)])
async def api_start_autobench(config: AutoBenchConfig):
    if config.start_gbps > config.max_gbps:
        raise HTTPException(status_code=400, detail="Стартовая скорость не может превышать максимальную")
    try:
        await run_in_trex_thread(trex_mgr.check_ports_link)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TRex недоступен: {e}")

    await _cancel_autobench()
    trex_mgr.autobench_task = asyncio.create_task(execute_autobench(config))
    return {"message": "Двухфазный автопоиск запущен"}


@app.post("/api/test/stop", dependencies=[Depends(require_token)])
async def api_stop_test():
    await _cancel_autobench()
    try:
        await run_in_trex_thread(trex_mgr.stop_test)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "Генерация остановлена"}


@app.post("/api/test/update", dependencies=[Depends(require_token)])
async def api_update_gbps(req: UpdateGbpsRequest):
    if trex_mgr.ab_active():
        raise HTTPException(status_code=409, detail="Идёт автопоиск — изменение нагрузки запрещено")
    try:
        applied = await run_in_trex_thread(trex_mgr.update_target_gbps, req.target_gbps)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if applied is None:
        raise HTTPException(status_code=409, detail="Тест не запущен — нечего масштабировать")
    return {"message": f"Нагрузка обновлена до {req.target_gbps} Гбит/с"}


@app.get("/api/reports/latest", dependencies=[Depends(require_token)])
async def api_latest_report():
    with report_lock:
        latest = report_mgr.latest_csv
        meta = dict(report_mgr.meta)
    return {
        "available": bool(latest and os.path.exists(latest)),
        "test_id": meta.get("test_id"),
        "summary": meta.get("summary", {}),
    }


@app.get("/api/reports/download/csv", dependencies=[Depends(require_token)])
async def api_download_csv():
    with report_lock:
        path = report_mgr.latest_csv
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Отчёт ещё не сформирован. Запустите и завершите тест.")
    return FileResponse(path=path, filename=os.path.basename(path), media_type="text/csv")


@app.get("/api/reports/download/pdf", dependencies=[Depends(require_token)])
async def api_download_pdf():
    with report_lock:
        path = report_mgr.latest_pdf
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Отчёт ещё не сформирован. Запустите и завершите тест.")
    return FileResponse(path=path, filename=os.path.basename(path), media_type="application/pdf")


@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket, token: Optional[str] = None):
    if not _token_ok(token or ""):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            data = await run_in_trex_thread(trex_mgr.get_telemetry)
            await websocket.send_json(data)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        traceback.print_exc()


@app.get("/")
async def get_dashboard():
    html_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="Файл index.html не найден рядом с server.py")
    return FileResponse(html_path, media_type="text/html")
