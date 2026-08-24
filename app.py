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
import json
import os
import socket
import threading
import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

import shtrih

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"

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

# Журнал последнего обмена с кассой — для разбора полётов в альфе.
LAST_EXCHANGE: list[str] = []

DEMO = False


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
    молча уехать на другой порт и сломать закладку.
    """
    return {"app": "kassa", "pid": os.getpid(), "demo": DEMO}


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
    if not DEMO and not load_config()["host"].strip():
        return {"online": False, "demo": False, "no_host": True,
                "error": "Адрес кассы не задан"}

    now = time.monotonic()
    cached = STATUS_CACHE["value"]
    if cached is not None and now - STATUS_CACHE["at"] < STATUS_TTL:
        return cached

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
            return {**cached, "busy": True}
        return {"online": False, "busy": True, "demo": DEMO,
                "error": "Касса занята другой операцией"}
    except (OSError, socket.timeout, shtrih.ProtocolError) as exc:
        value = {"online": False, "demo": DEMO, "error": str(exc)}
    except Exception as exc:
        raise _fail(exc)

    STATUS_CACHE["value"] = value
    STATUS_CACHE["at"] = time.monotonic()
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


@app.get("/api/log")
def protocol_log():
    """Журнал последнего обмена — чтобы было что показать при разборе сбоя."""
    return {"lines": LAST_EXCHANGE}


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
    return _simple(lambda k: k.open_shift(), "Смена открыта")


@app.post("/api/shift/close")
def close_shift():
    return _simple(lambda k: k.z_report(), "Смена закрыта, Z-отчёт напечатан")


@app.post("/api/report/x")
def x_report():
    return _simple(lambda k: k.x_report(), "X-отчёт напечатан")


@app.post("/api/receipt/cancel")
def cancel_receipt():
    return _simple(lambda k: k.cancel_receipt(), "Чек аннулирован")


@app.post("/api/receipt")
def punch_receipt(req: ReceiptRequest):
    if not req.positions:
        raise HTTPException(400, "В чеке нет ни одной позиции")
    if any(not p.name.strip() for p in req.positions):
        raise HTTPException(400, "У каждой позиции должно быть наименование")
    for p in req.positions:
        if p.vat not in shtrih.VAT_RATES:
            raise HTTPException(400, f"Неизвестная ставка НДС: {p.vat}")

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
