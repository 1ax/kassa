"""
Локальный веб-интерфейс к ККТ Штрих-М.

Запуск:
    python app.py               # порт подбирается сам, адрес печатается в консоль
    python app.py --port 9123   # фиксированный порт
    python app.py --demo        # без кассы, на эмуляторе

Сервер слушает только 127.0.0.1: интерфейс доступен с этого компьютера и ни
с какого другого.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import socket
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

import shtrih

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
CLOCK_LOG = BASE / "clock.log"

# Исходники, реально загруженные в память процесса. ui.html сюда НЕ входит:
# он читается с диска на каждый запрос (см. index()) и устареть не может.
SOURCE_FILES = (BASE / "app.py", BASE / "shtrih.py", BASE / "demo.py")

# Заготовка на случай, если config.json ещё нет и создать его не удалось.
# Адрес конкретной кассы живёт в config.json, а не здесь: держать его в коде —
# значит хранить настройку установки в репозитории.
DEFAULT_CONFIG = {
    "host": "",
    "port": 7778,
    "operator_password": 30,
    "admin_password": 30,
    "tax_system": "usn_income",
    "default_vat": "none",
    "default_payment_subject": 4,
}

# Порт 8000 занимают половина учебных примеров и локальных сервисов, 5000 и 7000
# на macOS держит AirPlay из Control Center. Берём заведомо тихий диапазон.
DEFAULT_HTTP_PORT = 8765

# Касса обслуживает один обмен за раз: два одновременных запроса рассыпают
# протокол (кадр одного уезжает в ответ другому). Все обращения — под замком.
KKT_LOCK = threading.Lock()

# Последний удачный статус: чтобы опрос приборной панели не вставал в очередь
# за печатью чека и не подвешивал интерфейс на минуту.
STATUS_CACHE: dict = {"at": 0.0, "value": None}
STATUS_TTL = 2.0

# Устарел ли код в памяти процесса относительно исходников на диске. Опрос
# приборной панели идёт раз в 5 секунд — кэшируем, чтобы не читать три файла
# на каждый тик.
_STALE_CACHE: dict = {"at": 0.0, "value": False}
STALE_TTL = 5.0

# Панель обслуживания опрашивается вручную (загрузка страницы + кнопка), а не
# по таймеру, поэтому кэш можно держать дольше, чем у приборной панели.
SERVICE_CACHE: dict = {"at": 0.0, "value": None}
SERVICE_TTL = 60.0

# Панель открывает человек, а не таймер, поэтому она вправе подождать замок,
# в отличие от приборной панели (там wait=0 — своё осознанное решение, не
# трогать). 15 секунд — компромисс: обычная гонка с опросом статуса при
# загрузке страницы (доли секунды) рассасывается сама, а на печатающей
# операции (до 90 секунд) панель честно сдаётся, а не висит.
SERVICE_WAIT = 15.0

# Журнал последнего обмена с кассой — для разбора полётов в альфе.
LAST_EXCHANGE: list[str] = []

DEMO = False


def _sources_digest() -> str:
    """
    Отпечаток исходников (SOURCE_FILES) в их текущем виде на диске.

    Любой сбой чтения (файл удалили, нет прав) — не повод падать: молча
    возвращаем "" («версия неизвестна»), а не роняем /api/ping или /api/status.
    """
    h = hashlib.sha256()
    try:
        for path in SOURCE_FILES:
            h.update(path.read_bytes())
    except OSError:
        return ""
    return h.hexdigest()


# Версия кода, реально загруженного в память этого процесса — считается один
# раз при импорте и больше не меняется, в отличие от _sources_digest(),
# которая каждый раз читает файлы заново.
CODE_VERSION = _sources_digest()


def is_stale() -> bool:
    """
    Устарел ли код в памяти процесса относительно исходников на диске —
    например, потому что лаунчер не перезапустил сервер, увидев, что порт
    уже отвечает.

    Пустой дайджест с любой стороны — «не знаем»: лучше не блокировать кассу
    зря, чем заблокировать её на ровном месте из-за нечитаемого файла.
    """
    now = time.monotonic()
    if now - _STALE_CACHE["at"] < STALE_TTL:
        return _STALE_CACHE["value"]
    current = _sources_digest()
    stale = bool(CODE_VERSION) and bool(current) and current != CODE_VERSION
    _STALE_CACHE["value"] = stale
    _STALE_CACHE["at"] = now
    return stale


# --- Конфигурация --------------------------------------------------------

def ensure_config() -> Path:
    """
    Создать config.json при первом запуске, чтобы настройки сразу лежали
    в файле, а не в коде. За образец берём config.example.json.
    """
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    sample = BASE / "config.example.json"
    cfg = dict(DEFAULT_CONFIG)
    if sample.exists():
        try:
            cfg.update(json.loads(sample.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    save_config(cfg)
    return CONFIG_PATH


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"config.json не прочитан ({exc}), беру значения по умолчанию")
    return cfg


def save_config(cfg: dict) -> None:
    """Пишем через временный файл: обрыв на записи не должен убить настройки."""
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)          # в файле пароли кассы
    tmp.replace(CONFIG_PATH)


def kkt():
    cfg = load_config()
    if DEMO:
        import demo
        return demo.DemoKKT()
    return shtrih.KKT(
        cfg["host"], cfg["port"],
        operator_password=cfg["operator_password"],
        admin_password=cfg["admin_password"],
    )


class Busy(Exception):
    """Касса занята другой операцией."""


def with_kkt(fn, *, wait: float = 120.0, record: bool = True):
    """
    Выполнить операцию на кассе под общим замком.

    record=False у фонового опроса статуса: иначе его кадры затирают журнал
    той самой операции, ради разбора которой журнал и заводился.
    """
    if not KKT_LOCK.acquire(timeout=wait):
        raise Busy()
    try:
        device = kkt()
        try:
            with device:
                return fn(device)
        finally:
            if record:
                LAST_EXCHANGE[:] = device.log[-60:]
    finally:
        KKT_LOCK.release()


# --- Модели запросов -----------------------------------------------------

class Position(BaseModel):
    name: str
    qty: float = Field(default=1, gt=0)
    price: float = Field(ge=0)
    vat: str = "none"
    payment_subject: int = 4
    payment_method: int = 4


class ReceiptRequest(BaseModel):
    op_type: int = shtrih.OP_INCOME
    positions: list[Position]
    cash: float = Field(default=0, ge=0)
    electronic: float = Field(default=0, ge=0)
    tax_system: str = "usn_income"
    text: str = ""
    corrected_fpd: str = ""


class CorrectionRequest(BaseModel):
    correction_type: int = 0
    op_type: int = shtrih.OP_INCOME
    total: float = Field(gt=0)
    cash: float = Field(default=0, ge=0)
    electronic: float = Field(default=0, ge=0)
    tax_system: str = "usn_income"
    reason_description: str = ""
    reason_date: str = Field(default_factory=lambda: date.today().isoformat())
    reason_number: str = ""


class ConfigRequest(BaseModel):
    host: str
    port: int = 7778
    operator_password: int = 30
    admin_password: int = 30
    tax_system: str = "usn_income"


# --- Журнал ухода часов ---------------------------------------------------
#
# Часы кассы отстают — на сколько именно, раньше было допущением (что при
# фискализации часы стояли точно). Здесь это допущение заменяется измерением:
# журнал накапливает замеры расхождения, а drift_rate() считает по ним
# скорость ухода. Журнал — вспомогательная вещь: сбой записи или чтения
# (нет прав, каталог вместо файла, битая строка) не должен ронять ни
# /api/status, ни /api/service, ни саму сверку часов.

# Момент последнего замера (по часам компьютера). None, пока не инициализирован;
# при первом обращении читаем последнюю строку файла, чтобы после перезапуска
# сервера отсчёт часа шёл от реальной последней записи, а не с нуля.
CLOCK_LOG_CACHE: dict = {"last_at": None, "initialized": False}

# Уход часов за час — сотые доли секунды, чаще писать журнал бессмысленно,
# а точек за неделю и так набирается достаточно для оценки скорости.
CLOCK_LOG_INTERVAL = 3600.0


def _read_clock_log(limit: int = 5000) -> list[dict]:
    """
    Хвост журнала замеров: последние `limit` строк.

    Файл растёт примерно на 9 тысяч строк в год — читать его целиком незачем,
    берём хвост через deque(maxlen=...). Ротации нет. Сбой чтения (файла нет,
    каталог вместо файла, нет прав) не должен ронять вызывающего — молча
    возвращаем что есть (в худшем случае пустой список), битые строки
    пропускаем.
    """
    try:
        with CLOCK_LOG.open("r", encoding="utf-8") as f:
            tail = collections.deque(f, maxlen=limit)
    except OSError:
        return []
    entries = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _clock_log_append(at: datetime, kkt_at: datetime, drift: float, *, kind: str | None = None) -> None:
    """
    Дописать в журнал одну строку замера расхождения часов.

    `kind="sync"` — отметка об успешной сверке (21h/22h/23h), с расхождением,
    которое было ДО сверки: после неё уход считается заново. У обычного
    замера ключа `kind` нет.

    Сбой записи (нет прав, каталог вместо файла, диск полон) не должен
    ронять вызывающего — молча проглатываем и продолжаем.
    """
    record = {
        "at": at.isoformat(timespec="seconds"),
        "kkt": kkt_at.isoformat(timespec="seconds"),
        "drift": drift,
    }
    if kind:
        record["kind"] = kind
    try:
        with CLOCK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _maybe_log_drift_sample(kkt_datetime: str) -> None:
    """
    Замер расхождения часов попутно со свежим /api/status — без единой лишней
    команды на кассу: 11h там и так уже опрашивается на приборную панель.
    Не чаще раза в час (CLOCK_LOG_INTERVAL).
    """
    try:
        kkt_at = datetime.strptime(kkt_datetime, "%d.%m.%Y %H:%M:%S")
    except (ValueError, TypeError):
        return
    at = datetime.now()

    if not CLOCK_LOG_CACHE["initialized"]:
        CLOCK_LOG_CACHE["initialized"] = True
        last = _read_clock_log(limit=1)
        if last:
            try:
                CLOCK_LOG_CACHE["last_at"] = datetime.fromisoformat(last[-1]["at"])
            except (KeyError, ValueError, TypeError):
                CLOCK_LOG_CACHE["last_at"] = None

    last_at = CLOCK_LOG_CACHE["last_at"]
    if last_at is not None and (at - last_at).total_seconds() < CLOCK_LOG_INTERVAL:
        return

    _clock_log_append(at, kkt_at, (kkt_at - at).total_seconds())
    CLOCK_LOG_CACHE["last_at"] = at


def drift_rate(entries: list[dict]) -> dict | None:
    """
    Скорость ухода часов кассы по крайним точкам журнала.

    Берём отрезок ПОСЛЕ последней отметки о сверке (`kind: sync`) — сама
    отметка в отрезок не входит; если сверок не было, берём весь журнал.
    Считаем по двум крайним точкам, а не регрессией: сверка — событие
    редкое, оценка «было — стало» по краям достаточно точна и её легко
    проверить глазами.

    Нужно минимум 3 точки и минимум 7 суток между первой и последней точкой
    отрезка — иначе данных мало, возвращаем None. Битые строки и записи
    без нужных ключей пропускаем, а не падаем.
    """
    tail: list[tuple[datetime, float]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == "sync":
            tail = []          # отрезок начинается заново после каждой сверки
            continue
        if "at" not in entry or "drift" not in entry:
            continue
        try:
            at = datetime.fromisoformat(entry["at"])
            drift = float(entry["drift"])
        except (KeyError, ValueError, TypeError):
            continue
        tail.append((at, drift))

    if len(tail) < 3:
        return None

    first_at, first_drift = tail[0]
    last_at, last_drift = tail[-1]
    span_days = (last_at - first_at).total_seconds() / 86400.0
    if span_days < 7:
        return None

    return {
        "rate": (last_drift - first_drift) / span_days,
        "days": span_days,
        "points": len(tail),
    }


def _drift_rate_fields() -> dict:
    """
    Поля скорости ухода для /api/service — считаются из журнала на диске,
    кассу не спрашивают. Отдельная функция, чтобы подмешать поля в любую
    ветку ответа, включая offline: журнал от кассы не зависит.
    """
    try:
        rate = drift_rate(_read_clock_log())
    except Exception:
        rate = None
    if rate is None:
        return {
            "clock_drift_rate": None,
            "clock_drift_rate_days": None,
            "clock_drift_rate_points": None,
        }
    return {
        "clock_drift_rate": rate["rate"],
        "clock_drift_rate_days": rate["days"],
        "clock_drift_rate_points": rate["points"],
    }


# --- Приложение ----------------------------------------------------------

app = FastAPI(title="Касса", docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    """
    Запросы принимаем только по локальному имени.

    Сокет и так слушает 127.0.0.1, но браузер по чужому DNS-имени, ведущему
    на 127.0.0.1, мог бы дотянуться до API со стороннего сайта. Проверка Host
    закрывает этот трюк.
    """
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost", "[::1]", "::1", ""):
        # Из middleware HTTPException не долетает до обработчика FastAPI —
        # ответ надо составить руками, иначе клиент получит 500.
        return PlainTextResponse(
            "Интерфейс доступен только с этого компьютера", status_code=403
        )
    return await call_next(request)


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, Busy):
        return HTTPException(409, "Касса занята другой операцией, подождите")
    if isinstance(exc, shtrih.KKTError):
        text = exc.name or "Касса отказала"
        return HTTPException(400, f"{text} (код 0x{exc.code:02X})")
    if isinstance(exc, shtrih.ProtocolError):
        return HTTPException(502, f"Сбой обмена с кассой: {exc}")
    if isinstance(exc, (OSError, socket.timeout)):
        return HTTPException(503, "Касса не отвечает по сети. Проверьте адрес и питание.")
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    return HTTPException(500, str(exc))


@app.get("/api/ping")
def ping():
    """
    Опознание запущенного экземпляра.

    Лаунчер стучится сюда перед стартом: если на порту уже наша касса —
    просто открыть браузер, если чужой сервис — сказать об этом, а не
    молча уехать на другой порт и сломать закладку. Поля version/stale —
    чтобы лаунчер узнал устаревший сервер и перезапустил его сам.
    """
    return {
        "app": "kassa", "pid": os.getpid(), "demo": DEMO,
        "version": CODE_VERSION[:12], "stale": is_stale(),
    }


@app.get("/api/config")
def get_config():
    cfg = load_config()
    cfg["demo"] = DEMO
    return cfg


@app.post("/api/config")
def set_config(req: ConfigRequest):
    if not req.host.strip():
        raise HTTPException(400, "Адрес кассы не может быть пустым")
    if req.tax_system not in shtrih.TAX_SYSTEMS:
        raise HTTPException(400, f"Неизвестная система налогообложения: {req.tax_system}")
    if not 1 <= req.port <= 65535:
        raise HTTPException(400, "Порт вне диапазона 1–65535")
    cfg = load_config()
    cfg.update(req.model_dump())
    save_config(cfg)
    STATUS_CACHE["at"] = 0.0
    return cfg


@app.get("/api/status")
def status():
    """
    Приборная панель. Опрашивается интерфейсом по таймеру, поэтому:
      * если касса занята печатью — сразу отдаём последний известный статус,
        а не ждём освобождения замка;
      * свежий ответ держим пару секунд, чтобы не долбить кассу пятью
        командами на каждый тик.
    """
    stale = is_stale()
    if not DEMO and not load_config()["host"].strip():
        return {"online": False, "demo": False, "no_host": True,
                "error": "Адрес кассы не задан", "stale": stale}

    now = time.monotonic()
    cached = STATUS_CACHE["value"]
    if cached is not None and now - STATUS_CACHE["at"] < STATUS_TTL:
        return {**cached, "stale": stale}

    def read(k):
        short = k.short_status()
        shift = k.shift_params()
        fn = k.fn_status()
        ofd = k.ofd_status()
        long = k.long_status()
        return {
            "online": True,
            "demo": DEMO,
            "mode": short["mode_name"],
            "mode_code": short["mode"],
            "receipt_open": short["receipt_open"],
            "paper": short["paper"],
            "flags": short["flags_hex"],
            "shift_open": shift["shift_open"],
            "shift_number": shift["shift_number"],
            "receipt_number": shift["receipt_number"],
            "fn_number": fn["fn_number"],
            "last_fd": fn["last_fd"],
            "current_document": fn["current_document"],
            "ofd_queue": ofd["queue_length"],
            "ofd_connected": ofd["connected"],
            "serial": long["serial"],
            "datetime": f"{long['date']} {long['time']}",
        }

    try:
        value = with_kkt(read, wait=0, record=False)
    except Busy:
        if cached is not None:
            return {**cached, "busy": True, "stale": stale}
        return {"online": False, "busy": True, "demo": DEMO,
                "error": "Касса занята другой операцией", "stale": stale}
    except (OSError, socket.timeout, shtrih.ProtocolError) as exc:
        value = {"online": False, "demo": DEMO, "error": str(exc)}
    except Exception as exc:
        raise _fail(exc)

    STATUS_CACHE["value"] = value
    STATUS_CACHE["at"] = time.monotonic()
    if "datetime" in value:
        # Только на пути успешного свежего ответа — не из кэша и не с ветки
        # ошибки/занятости, где часов кассы мы не читали.
        try:
            _maybe_log_drift_sample(value["datetime"])
        except Exception:
            pass
    return {**value, "stale": stale}


def days_left(expiry: str, today: date | None = None) -> int | None:
    """
    Сколько дней осталось до истечения срока действия ФН.

    `expiry` — строка «ДД.ММ.ГГГГ», как её возвращает shtrih.fn_expiry().
    Неразбираемая строка — не повод падать: возвращаем None, предупреждение
    в этом случае не поднимается.
    """
    try:
        d = datetime.strptime(expiry, "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None
    return (d - (today or date.today())).days


@app.get("/api/service")
def service():
    """
    Панель обслуживания: срок действия ФН, версия его ПО, итоги последней
    фискализации, число ФД без квитанции ОФД.

    Запрашивается интерфейсом один раз при загрузке и по кнопке «Обновить»,
    не по таймеру — поэтому кэш держим дольше, чем у /api/status, а не
    гоняем эти команды на каждый тик приборной панели.
    """
    if not DEMO and not load_config()["host"].strip():
        return {"online": False, "demo": False, "no_host": True,
                "error": "Адрес кассы не задан", **_drift_rate_fields()}

    now = time.monotonic()
    cached = SERVICE_CACHE["value"]
    if cached is not None and now - SERVICE_CACHE["at"] < SERVICE_TTL:
        return cached

    def read(k):
        expiry = k.fn_expiry()
        version = k.fn_version()
        fiscal = k.fiscalization()
        unconfirmed = k.unconfirmed_documents()
        days = days_left(expiry["expiry"])
        return {
            "online": True,
            "demo": DEMO,
            "fn_expiry": expiry["expiry"],
            "fn_expiry_days": days,
            "fn_expiry_warning": days is not None and days < 30,
            "fn_version": version["version"],
            "fn_serial_software": version["serial_software"],
            "fiscalization": fiscal,
            "unconfirmed": unconfirmed,
            "unconfirmed_warning": unconfirmed > 0,
        }

    try:
        value = with_kkt(read, wait=SERVICE_WAIT, record=False)
    except Busy:
        if cached is not None:
            return {**cached, "busy": True}
        return {"online": False, "busy": True, "demo": DEMO,
                "error": "Касса занята другой операцией", **_drift_rate_fields()}
    except (OSError, socket.timeout, shtrih.ProtocolError) as exc:
        value = {"online": False, "demo": DEMO, "error": str(exc)}
    except Exception as exc:
        raise _fail(exc)

    # Скорость ухода — из журнала на диске, кассу не спрашивает, поэтому
    # попадает в ответ и на ветке online: false (журнал от связи не зависит).
    value.update(_drift_rate_fields())
    SERVICE_CACHE["value"] = value
    SERVICE_CACHE["at"] = time.monotonic()
    return value


@app.get("/api/device")
def device():
    try:
        return with_kkt(lambda k: k.device_type(), wait=5)
    except Exception as exc:
        raise _fail(exc)


def terminate() -> None:
    """Вынесено отдельно, чтобы тест мог подменить и не погасить сам себя."""
    import signal

    os.kill(os.getpid(), signal.SIGTERM)


@app.post("/api/quit")
def quit_server():
    """
    Погасить сервер по кнопке в интерфейсе.

    Запускается через иконку, а не из терминала, поэтому гасить его иначе
    было бы нечем. Пока идёт обмен с кассой — отказываемся: обрывать
    открытый чек на полпути нельзя.
    """
    if not KKT_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Касса занята операцией, подождите её окончания")
    KKT_LOCK.release()

    def shutdown():
        time.sleep(0.3)          # даём ответу дойти до браузера
        terminate()

    threading.Thread(target=shutdown, daemon=True).start()
    return {"ok": True, "message": "Сервер остановлен"}


def restart() -> None:
    """Вынесено отдельно, чтобы тест мог подменить и не перезапустить сам себя."""
    os.chdir(BASE)
    os.execv(sys.executable, [sys.executable, *sys.argv])


@app.post("/api/restart")
def restart_server():
    """
    Перезапустить сервер по кнопке в интерфейсе — на устаревшем сервере
    (код в памяти старше исходников на диске) или по желанию владельца.
    Пока идёт обмен с кассой — отказываемся: обрывать печать чека
    перезапуском нельзя.
    """
    if not KKT_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Касса занята операцией, подождите её окончания")
    KKT_LOCK.release()

    def do_restart():
        time.sleep(0.3)          # даём ответу дойти до браузера
        restart()

    threading.Thread(target=do_restart, daemon=True).start()
    return {"ok": True, "message": "Сервер перезапускается"}


@app.get("/api/log")
def protocol_log():
    """Журнал последнего обмена — чтобы было что показать при разборе сбоя."""
    return {"lines": LAST_EXCHANGE}


def _refuse_if_stale() -> None:
    """
    Не пускать печатающую операцию на сервер, чьи исходники в памяти
    устарели относительно диска: отдаём внятную ошибку с указанием кнопки
    перезапуска вместо того, чтобы выполнить операцию кодом, которого уже
    никто не видит.
    """
    if is_stale():
        raise HTTPException(
            409,
            "Запущен старый сервер, код на диске новее. "
            "Нажмите «Перезапустить кассу» вверху страницы.",
        )


def _simple(action, ok_text: str):
    STATUS_CACHE["at"] = 0.0
    try:
        with_kkt(action)
        return {"ok": True, "message": ok_text}
    except Exception as exc:
        raise _fail(exc)
    finally:
        STATUS_CACHE["at"] = 0.0


@app.post("/api/shift/open")
def open_shift():
    _refuse_if_stale()
    return _simple(lambda k: k.open_shift(), "Смена открыта")


@app.post("/api/shift/close")
def close_shift():
    # НЕ вызывать _refuse_if_stale(): если код на диске обновится при
    # открытой смене, заблокированный Z-отчёт через 24 часа уведёт кассу
    # в режим 3 (касса откажется работать вообще). Это аварийный выход,
    # доступный всегда — независимо от версии кода в памяти процесса.
    return _simple(lambda k: k.z_report(), "Смена закрыта, Z-отчёт напечатан")


@app.post("/api/report/x")
def x_report():
    _refuse_if_stale()
    return _simple(lambda k: k.x_report(), "X-отчёт напечатан")


@app.post("/api/receipt/cancel")
def cancel_receipt():
    # НЕ вызывать _refuse_if_stale(): заблокированное аннулирование оставит
    # висеть открытый чек, который потом будет нечем закрыть. Это аварийный
    # выход, доступный всегда — независимо от версии кода в памяти процесса.
    return _simple(lambda k: k.cancel_receipt(), "Чек аннулирован")


def _clock_state(k) -> dict:
    """
    Прочитать всё, что нужно ограждениям сверки часов, — только читающими
    командами (10h/11h/FF01h/FF40h), ни одна из них ничего не печатает.
    """
    short = k.short_status()
    shift = k.shift_params()
    fn = k.fn_status()
    long = k.long_status()
    try:
        now = datetime.strptime(f"{long['date']} {long['time']}", "%d.%m.%Y %H:%M:%S")
    except (ValueError, TypeError):
        raise ValueError(
            f"Касса вернула нечитаемые дату и время: «{long['date']} {long['time']}». "
            "Похоже на сброс часов после севшей батарейки — кнопкой сверки тут не "
            "обойтись, нужен сервис."
        )
    try:
        last_document_at = datetime.strptime(fn["last_document_at"], "%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        # Строка не разобралась — не роняем сверку, но и не снимаем ограждение:
        # просто не проверяем расхождение с последним ФД в этом заходе.
        last_document_at = None
    return {
        "mode": short["mode"],
        "shift_open": shift["shift_open"],
        "receipt_open": short["receipt_open"],
        "now": now,
        "last_document_at": last_document_at,
    }


def _guard_shift_receipt_drift(state: dict, now: datetime) -> None:
    """Ограждения 2-4: открытая смена, открытый чек, расхождение больше суток."""
    if state["shift_open"]:
        raise ValueError(
            "Смена открыта. Сначала закройте её Z-отчётом, потом сверяйте часы."
        )
    if state["receipt_open"]:
        raise ValueError(
            "Открыт чек. Сначала аннулируйте его, потом сверяйте часы."
        )
    drift = (state["now"] - now).total_seconds()
    if abs(drift) > 24 * 3600:
        raise ValueError(
            "Расхождение часов кассы больше суток — это не уход кварца, "
            "а сброс часов. Разбираться нужно вручную."
        )


def _guard_last_document(state: dict, now: datetime) -> None:
    """Ограждение 5: новое время не должно быть раньше момента последнего ФД."""
    if state["last_document_at"] is not None and now < state["last_document_at"]:
        raise ValueError(
            "Новое время раньше момента последнего фискального документа. "
            "ФН всё равно откажет с ошибкой «Неверные дата и/или время»."
        )


def _clock_result(was: datetime, now: datetime, message: str) -> dict:
    return {
        "ok": True,
        "message": message,
        "was": was.strftime("%d.%m.%Y %H:%M:%S"),
        "now": now.strftime("%d.%m.%Y %H:%M:%S"),
        "drift_seconds": (was - now).total_seconds(),
    }


@app.post("/api/clock/time")
def clock_time():
    """Сверить время кассы (21h) с часами этого компьютера."""
    _refuse_if_stale()
    STATUS_CACHE["at"] = 0.0
    try:
        def run(k):
            now = datetime.now()
            state = _clock_state(k)
            if state["mode"] == 6:
                raise ValueError(
                    "Касса ждёт подтверждения даты. Сначала нажмите «Сверить дату»."
                )
            _guard_shift_receipt_drift(state, now)
            _guard_last_document(state, now)
            k.set_time(now.time())
            return state["now"], now

        was, now = with_kkt(run)
        result = _clock_result(was, now, "Время кассы сверено")
        try:
            _clock_log_append(now, was, result["drift_seconds"], kind="sync")
        except Exception:
            pass
        return result
    except Exception as exc:
        raise _fail(exc)
    finally:
        STATUS_CACHE["at"] = 0.0


@app.post("/api/clock/date")
def clock_date():
    """Сверить дату кассы (22h+23h) с часами этого компьютера."""
    _refuse_if_stale()
    STATUS_CACHE["at"] = 0.0
    try:
        def run(k):
            now = datetime.now()
            state = _clock_state(k)
            if state["mode"] == 6:
                # Смена в режиме 6 закрыта по определению — ограждения 2-4
                # не нужны, но расхождение с последним ФД всё ещё проверяем.
                _guard_last_document(state, now)
                k.confirm_date(now.date())
            else:
                _guard_shift_receipt_drift(state, now)
                _guard_last_document(state, now)
                k.set_date(now.date())
            return state["now"], now

        was, now = with_kkt(run)
        result = _clock_result(was, now, "Дата кассы сверена")
        try:
            _clock_log_append(now, was, result["drift_seconds"], kind="sync")
        except Exception:
            pass
        return result
    except Exception as exc:
        raise _fail(exc)
    finally:
        STATUS_CACHE["at"] = 0.0


@app.post("/api/receipt")
def punch_receipt(req: ReceiptRequest):
    _refuse_if_stale()
    if not req.positions:
        raise HTTPException(400, "В чеке нет ни одной позиции")
    if any(not p.name.strip() for p in req.positions):
        raise HTTPException(400, "У каждой позиции должно быть наименование")
    for p in req.positions:
        if p.vat not in shtrih.VAT_RATES:
            raise HTTPException(400, f"Неизвестная ставка НДС: {p.vat}")
    corrected_fpd = req.corrected_fpd.strip()
    if corrected_fpd and not corrected_fpd.isdigit():
        raise HTTPException(400, "ФПД исправляемого чека состоит только из цифр")

    total = round(sum(p.qty * p.price for p in req.positions), 2)
    if total <= 0:
        raise HTTPException(400, "Сумма чека — ноль. Проверьте цены и количество.")
    paid = round(req.cash + req.electronic, 2)
    if paid + 0.01 < total:
        raise HTTPException(400, f"Оплата {paid:.2f} меньше суммы чека {total:.2f}")
    if req.electronic > total + 0.01:
        raise HTTPException(
            400, "Электронными нельзя заплатить больше суммы чека: сдачи с них не бывает"
        )

    doc_type = {
        shtrih.OP_INCOME: shtrih.DOC_SALE,
        shtrih.OP_INCOME_RETURN: shtrih.DOC_SALE_RETURN,
        shtrih.OP_EXPENSE: shtrih.DOC_BUY,
        shtrih.OP_EXPENSE_RETURN: shtrih.DOC_BUY_RETURN,
    }.get(req.op_type)
    if doc_type is None:
        raise HTTPException(400, f"Неизвестный тип операции: {req.op_type}")

    def run(k):
        k.open_receipt(doc_type)
        try:
            if corrected_fpd:
                k.send_tlv(shtrih.corrected_receipt_tlv(corrected_fpd))
            for p in req.positions:
                k.operation(
                    op_type=req.op_type,
                    qty=p.qty,
                    price=p.price,
                    name=p.name,
                    vat=p.vat,
                    payment_method=p.payment_method,
                    payment_subject=p.payment_subject,
                )
            return k.close_receipt(
                cash=req.cash,
                electronic=req.electronic,
                tax_system=req.tax_system,
                text=req.text,
            )
        except Exception:
            # Не оставляем открытый чек висеть в кассе
            try:
                k.cancel_receipt()
            except Exception:
                pass
            raise

    STATUS_CACHE["at"] = 0.0
    try:
        return {"ok": True, **with_kkt(run)}
    except Exception as exc:
        raise _fail(exc)
    finally:
        STATUS_CACHE["at"] = 0.0


@app.post("/api/correction")
def punch_correction(req: CorrectionRequest):
    _refuse_if_stale()
    if req.op_type not in (shtrih.OP_INCOME, shtrih.OP_EXPENSE):
        raise HTTPException(
            400,
            "В ФФД 1.05 у чека коррекции допустимы только приход и расход. "
            "Ошибочный чек исправляется чеком возврата прихода.",
        )
    paid = round(req.cash + req.electronic, 2)
    if abs(paid - req.total) > 0.01:
        raise HTTPException(
            400,
            f"Суммы оплаты ({paid:.2f}) не сходятся с суммой расчёта ({req.total:.2f})",
        )
    if not req.reason_description.strip():
        raise HTTPException(
            400, "Без описания основания чек коррекции налоговая вправе не признать"
        )
    try:
        reason_date = date.fromisoformat(req.reason_date)
    except ValueError:
        raise HTTPException(400, "Дата документа основания не разобрана")

    STATUS_CACHE["at"] = 0.0
    try:
        result = with_kkt(lambda k: k.correction(
            correction_type=req.correction_type,
            op_type=req.op_type,
            total=req.total,
            cash=req.cash,
            electronic=req.electronic,
            tax_system=req.tax_system,
            reason_description=req.reason_description,
            reason_date=reason_date,
            reason_number=req.reason_number,
        ))
        return {"ok": True, **result}
    except Exception as exc:
        raise _fail(exc)
    finally:
        STATUS_CACHE["at"] = 0.0


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "ui.html").read_text(encoding="utf-8")


# --- Запуск --------------------------------------------------------------

def running_instance(port: int, timeout: float = 0.6) -> dict | None:
    """
    Кто занял порт: наша касса, кто-то чужой или никто.

    Возвращает ответ /api/ping, если это наш сервер; {} — если порт занят
    чем-то посторонним; None — если порт свободен.
    """
    with socket.socket() as probe:
        probe.settimeout(timeout)
        if probe.connect_ex(("127.0.0.1", port)) != 0:
            return None
    try:
        import urllib.request
        # Мимо прокси: если в окружении стоит HTTP_PROXY, urllib потащит
        # через него даже запрос на 127.0.0.1 — и свой же сервер опознать
        # не сможет. Ровно на этом --stop однажды объявил кассу «чужим сервисом».
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{port}/api/ping", timeout=timeout
        ) as r:
            data = json.loads(r.read())
        return data if data.get("app") == "kassa" else {}
    except Exception:
        return {}


def stop_instance(port: int) -> int:
    """Погасить запущенную кассу. Возвращает код возврата для оболочки."""
    import signal

    running = running_instance(port)
    if running is None:
        print(f"На порту {port} никто не слушает — гасить нечего.")
        return 0
    if not running:
        print(f"Порт {port} занят посторонним сервисом, не трогаю его.")
        return 1
    pid = running["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("Процесс уже завершился.")
        return 0
    except PermissionError:
        print(f"Нет прав погасить процесс {pid}.")
        return 1
    print(f"Касса остановлена (процесс {pid}).")
    return 0


def free_port(preferred: int, tries: int = 20) -> int:
    """Первый свободный порт, начиная с предпочтительного."""
    for candidate in range(preferred, preferred + tries):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(
        f"Свободного порта нет в диапазоне {preferred}-{preferred + tries - 1}. "
        f"Укажите свой: python app.py --port НОМЕР"
    )


def main() -> None:
    global DEMO

    ap = argparse.ArgumentParser(description="Веб-интерфейс к ККТ Штрих-М")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("KASSA_PORT", DEFAULT_HTTP_PORT)),
                    help=f"порт веб-интерфейса (по умолчанию {DEFAULT_HTTP_PORT})")
    ap.add_argument("--strict-port", action="store_true",
                    help="не искать свободный порт, падать если занят")
    ap.add_argument("--demo", action="store_true",
                    help="работать на эмуляторе кассы, ничего не печатать")
    ap.add_argument("--open", action="store_true",
                    help="открыть интерфейс в браузере сразу после старта")
    ap.add_argument("--stop", action="store_true",
                    help="погасить уже запущенную кассу и выйти")
    args = ap.parse_args()

    if args.stop:
        raise SystemExit(stop_instance(args.port))

    DEMO = args.demo
    ensure_config()

    # На фиксированном порту может уже сидеть наш же сервер — тогда второй
    # поднимать незачем: закладка и иконка ведут на тот же адрес.
    running = running_instance(args.port)
    if running:
        url = f"http://127.0.0.1:{args.port}"
        print(f"Касса уже запущена: {url}", flush=True)
        if args.open:
            import webbrowser
            webbrowser.open(url)
        return
    if running == {}:
        if args.strict_port:
            raise SystemExit(
                f"Порт {args.port} занят посторонним сервисом. "
                f"Освободите его или укажите другой: --port НОМЕР"
            )
        print(f"Порт {args.port} занят посторонним сервисом, ищу свободный.")

    port = args.port if args.strict_port else free_port(args.port)

    url = f"http://127.0.0.1:{port}"
    cfg = load_config()
    banner = ["", f"  Касса     {url}"]
    if DEMO:
        banner.append("  Режим     ДЕМО — касса эмулируется, ничего не печатается")
    else:
        banner.append(f"  ККТ       {cfg['host']}:{cfg['port']}")
    if port != args.port:
        banner.append(f"  Порт      {args.port} занят, взял {port}")
    banner += ["  Остановка Ctrl+C", ""]
    print("\n".join(banner), flush=True)   # без flush адрес тонет в буфере

    if args.open:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
