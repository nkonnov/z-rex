"""Тесты ReportManager: жизненный цикл, гонки, уникальность test_id, сводка."""
import os
import threading

import report_generator
from report_generator import ReportManager


def make_mgr(tmp_path):
    return ReportManager(reports_dir=str(tmp_path / "reports"))


def point(tx=1.0, rx=0.9, t="00:00:01"):
    return {"timestamp": t, "global": {"tx_gbps": tx, "rx_gbps": rx, "tx_mpps": 0.1},
            "l7_client": {"active_flows": 5, "est_flows": 4, "conn_drops": 0, "syn_timeouts": 0}}


def test_lifecycle_and_files(tmp_path):
    mgr = make_mgr(tmp_path)
    sid = mgr.start_session(mode="manual", params={"target_gbps": 2.0})
    assert sid
    for i in range(5):
        assert mgr.add_point(point(t=f"00:00:0{i}")) == sid
    mgr.finish_session(summary={"extra": 1}, session_id=sid)

    assert not mgr.is_recording_now()
    assert mgr.add_point(point()) is None  # после закрытия не пишем

    csv_path, pdf_path = mgr.latest_paths()
    assert csv_path and os.path.exists(csv_path)
    assert pdf_path and os.path.exists(pdf_path)
    assert sid in os.path.basename(csv_path)

    meta = mgr.meta_snapshot()
    s = meta["summary"]
    assert s["samples_count"] == 5
    assert s["peak_tx_gbps"] == 1.0
    assert s["extra"] == 1
    assert meta["finished_at"]


def test_finish_with_stale_session_id_does_not_close_new(tmp_path):
    """Гонка 4.2: между чтением session_id и finish сессию уже сменили."""
    mgr = make_mgr(tmp_path)
    sid_old = mgr.start_session(mode="manual", params={})
    mgr.add_point(point())
    sid_new = mgr.start_session(mode="autobench", params={})  # закроет старую внутри
    assert sid_new != sid_old

    mgr.finish_session(summary={"late": True}, session_id=sid_old)  # запаздывающий stop
    assert mgr.is_recording_now(), "поздний finish не должен закрыть новую сессию"

    mgr.finish_session(session_id=sid_new)
    assert not mgr.is_recording_now()


def test_test_id_unique_within_same_second(tmp_path, monkeypatch):
    """Две сессии в одну секунду: test_id и файлы отчётов не должны коллидировать."""
    # *args/**kwargs: strftime используется и внутри fpdf с дополнительными аргументами.
    monkeypatch.setattr(report_generator.time, "strftime",
                        lambda fmt, *args, **kwargs: "20261120_120000")
    mgr = make_mgr(tmp_path)
    sid1 = mgr.start_session(mode="a", params={})
    sid2 = mgr.start_session(mode="b", params={})
    assert sid1 != sid2
    mgr.add_point(point())
    mgr.finish_session(session_id=sid2)
    mgr.finish_session(session_id=sid1)  # первая уже закрыта внутри start_session — no-op
    csv_last, _ = mgr.latest_paths()
    assert sid2 in os.path.basename(csv_last)
    # Оба отчёта лежат рядом на диске: коллизия имён не перезаписала первый.
    files = os.listdir(mgr.reports_dir)
    assert any(sid1 in f for f in files)
    assert any(sid2 in f for f in files)


def test_concurrent_lifecycle(tmp_path):
    """Бомбардировка start/add/finish из 8 потоков — без дедлоков и ошибок."""
    mgr = make_mgr(tmp_path)
    # Файловую запись отключаем: проверяем потокобезопасность цикла, не скорость PDF.
    monkey_calls = {"csv": 0, "pdf": 0}
    mgr._write_csv = lambda path: monkey_calls.__setitem__("csv", monkey_calls["csv"] + 1)
    mgr._write_pdf = lambda path: monkey_calls.__setitem__("pdf", monkey_calls["pdf"] + 1)

    errors = []

    def worker(n):
        try:
            for _ in range(50):
                sid = mgr.start_session(mode="t", params={}) if n % 3 == 0 else None
                mgr.add_point(point(), None)
                if sid:
                    mgr.finish_session(session_id=sid)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert monkey_calls["csv"] == monkey_calls["pdf"] > 0
