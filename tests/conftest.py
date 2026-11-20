"""Общая настройка тестов: всегда работаем на встроенной заглушке TRex.

TREX_PATH принудительно указывает на tests/trex_stub даже на машине с реальной
установкой /opt/trex — юнит-тесты не должны трогать генератор.
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)

os.environ["TREX_PATH"] = os.path.join(TESTS_DIR, "trex_stub")
sys.path.insert(0, PROJECT_ROOT)

# Отчёты тестов пишем во временную папку, не в reports/ проекта.
os.environ.setdefault("NETSTRIKE_REPORTS_DIR", os.path.join(TESTS_DIR, "_reports"))
