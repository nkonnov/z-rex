"""Тест калибровки «mult → Гбит/с» после ручного старта (P1-4)."""
import server


class FakeClient:
    def __init__(self, active=True):
        self._active = active
        self.updates = []

    def is_traffic_active(self):
        return self._active

    def update(self, mult=None, **kw):
        self.updates.append(mult)


def make_mgr(tmp_path):
    m = server.TRexManager()
    m._active_profile = server.PROFILE_CUSTOM
    m._mult_per_gbps[server.PROFILE_CUSTOM] = 1.0  # грубая оценка: 1 mult = 1 Гбит/с
    return m


def test_manual_calibration_adjusts_mult_to_target(tmp_path):
    """Факт 2 Гбит/с при mult=1 → коэффициент 0.5, mult до-подстроен до 5*0.5=2.5."""
    m = make_mgr(tmp_path)
    m.client = FakeClient()
    m._last_applied_mult = 1.0
    m._manual_calib_pending = True
    m._manual_target_gbps = 5.0

    m._maybe_calibrate_manual(tx_gbps=2.0)

    assert not m._manual_calib_pending          # калибровка однократна
    assert m._mult_per_gbps[server.PROFILE_CUSTOM] == 0.5
    assert m.client.updates == [2.5]            # до-подстройка до целевой полосы
    assert m._last_applied_mult == 2.5

    # Повторный тик ничего не меняет.
    m._maybe_calibrate_manual(tx_gbps=2.6)
    assert m.client.updates == [2.5]


def test_manual_calibration_skips_noise(tmp_path):
    """Тики с полосой ниже порога шума игнорируются до фактического значения."""
    m = make_mgr(tmp_path)
    m.client = FakeClient()
    m._last_applied_mult = 1.0
    m._manual_calib_pending = True
    m._manual_target_gbps = 5.0

    m._maybe_calibrate_manual(tx_gbps=0.05)  # ниже MIN_RATE_GBPS
    assert m._manual_calib_pending
    assert m.client.updates == []

    m._maybe_calibrate_manual(tx_gbps=3.0)
    assert not m._manual_calib_pending
    assert m.client.updates == [round(5.0 * (1.0 / 3.0), 4)]


def test_manual_calibration_no_update_when_estimate_was_right(tmp_path):
    """Если оценка совпала с фактом (mult сразу дал целевую полосу), mult не дёргаем."""
    m = make_mgr(tmp_path)
    m.client = FakeClient()
    m._last_applied_mult = 4.0
    m._manual_calib_pending = True
    m._manual_target_gbps = 4.0

    m._maybe_calibrate_manual(tx_gbps=4.0)  # факт ровно равен целевой

    assert m.client.updates == []  # коэффициент не изменился — update не нужен
