"""Единая точка определения пути к TRex и подключения его Python-API.

Порядок поиска TRex:
  1. переменная окружения TREX_PATH (например, export TREX_PATH=/opt/trex/v3.04);
  2. самая новая версия в /opt/trex/vX.YY.

Импортируйте этот модуль ПЕРВЫМ в любом скрипте, работающем с TRex:
    import trex_setup                      # настраивает sys.path
    from trex.astf.api import ASTFClient   # затем импорт API
"""
import glob
import os
import re
import sys

__all__ = ["TREX_PATH"]

TREX_ROOT = "/opt/trex"


def _version_key(path: str):
    m = re.search(r"v(\d+)\.(\d+)", os.path.basename(path))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _find_trex_path() -> str:
    env = os.environ.get("TREX_PATH", "").strip()
    if env:
        if not os.path.isdir(env):
            raise RuntimeError(f"TREX_PATH={env!r} не существует или не является папкой")
        return env

    candidates = [p for p in glob.glob(os.path.join(TREX_ROOT, "v*")) if os.path.isdir(p)]
    if not candidates:
        raise RuntimeError(
            f"Установка TRex не найдена в {TREX_ROOT}/v*. "
            f"Задайте путь через переменную окружения TREX_PATH."
        )
    return max(candidates, key=_version_key)


TREX_PATH = _find_trex_path()

_API_PATHS = (
    os.path.join(TREX_PATH, "automation", "trex_control_plane", "interactive"),
    os.path.join(TREX_PATH, "external_libs"),
    TREX_PATH,
)

# Вставляем в обратном порядке, чтобы итоговый порядок совпадал с _API_PATHS.
for _p in reversed(_API_PATHS):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import trex.astf.api  # noqa: F401  проверка, что API доступно
except ImportError as e:
    raise RuntimeError(
        f"Не удалось импортировать trex.astf.api из {TREX_PATH}: {e}. "
        f"Проверьте, что установлена корректная версия TRex."
    ) from e
