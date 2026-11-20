"""Тесты методики: _verdict (три критерия провала) и _estimate_base_gbps."""
import server
from server import AutoBenchConfig, StepResult, TestConfig, _estimate_base_gbps, _verdict


def step(tx=10.0, rx=9.8, mpps=1.0, drop_pct=0.0, drops=0):
    return StepResult(applied_mult=1.0, tx_gbps=tx, rx_gbps=rx, tx_mpps=mpps,
                      drop_pct=drop_pct, drops_delta=drops)


def verdict(r, tol=3.0, drop_limit=2.0, peak=0.0):
    return _verdict(r, bw_tolerance_pct=tol, drop_limit=drop_limit, peak_rx=peak)


def test_verdict_clean_pass():
    assert verdict(step()) is None


def test_verdict_rx_below_tolerance():
    r = step(tx=10.0, rx=9.0)  # 10% < RX при допуске 3%
    reason = verdict(r, tol=3.0)
    assert reason and "потеря полосы" in reason


def test_verdict_rx_tolerance_boundary_passes():
    # RX = TX*(1-3%) ровно на границе — ещё не провал (строгое сравнение).
    r = step(tx=10.0, rx=10.0 * 0.97)
    assert verdict(r, tol=3.0) is None


def test_verdict_small_rates_skip_bandwidth_check():
    # Ниже MIN_RATE_GBPS=0.1 полосу считаем шумом — критерий полосы не срабатывает.
    r = step(tx=0.05, rx=0.0)
    assert verdict(r, tol=3.0) is None


def test_verdict_degradation_against_peak():
    peak = 10.0  # > MIN_PEAK_FOR_DEGRADATION=0.5
    # RX в допуске относительно TX (8.8 > 9.0*0.97), но провален против пика (8.8 < 10*0.92).
    r = step(tx=9.0, rx=8.8)
    reason = verdict(r, peak=peak)
    assert reason and "деградация" in reason


def test_verdict_degradation_disabled_for_small_peak():
    # Пик 0.3 < 0.5: деградацию не проверяем, хотя RX и ниже пика*0.92.
    # Полоса при этом чистая: rx=0.272 >= tx*0.97=0.2716.
    r = step(tx=0.28, rx=0.272)
    assert verdict(r, peak=0.3) is None


def test_verdict_drop_pct_over_limit():
    r = step(drop_pct=2.5, drops=25)
    reason = verdict(r, drop_limit=2.0)
    assert reason and "потери" in reason


def test_verdict_bandwidth_has_priority_over_degradation():
    # Провал по полоса+деградация одновременно — первым сообщается критерий полосы.
    r = step(tx=10.0, rx=5.0)
    reason = verdict(r, peak=10.0)
    assert reason and "потеря полосы" in reason


def test_autobench_config_defaults():
    cfg = AutoBenchConfig()
    assert cfg.bw_tolerance_pct == 3.0
    assert cfg.baseline_max_drop_pct == 5.0
    assert cfg.confirm_failures is True


def test_estimate_base_gbps_positive_and_grows_with_payload():
    base = _estimate_base_gbps(TestConfig())
    bigger = _estimate_base_gbps(TestConfig(http_payload_kb=150))
    assert 0.001 < base < 100.0
    assert bigger > base * 8  # полезная нагрузка доминирует в оценке
