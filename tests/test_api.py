"""
Проверки HTTP-слоя на эмуляторе кассы.

Живая ККТ здесь не нужна и не должна быть нужна: тесты гоняются в демо-режиме,
ничего не печатают и никуда не отправляют.
"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app as kassa_app
import demo
import shtrih


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(kassa_app, "DEMO", True)
    monkeypatch.setattr(kassa_app, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(kassa_app, "CLOCK_LOG", tmp_path / "clock.log")
    kassa_app.STATUS_CACHE["value"] = None
    kassa_app.STATUS_CACHE["at"] = 0.0
    kassa_app._STALE_CACHE["at"] = 0.0
    kassa_app._STALE_CACHE["value"] = False
    kassa_app.CLOCK_LOG_CACHE["last_at"] = None
    kassa_app.CLOCK_LOG_CACHE["initialized"] = False
    # Кэши /api/service и /api/ffd, а с ними и FFD_STATE защёлки перед
    # печатью — иначе переключение demo.DemoKKT.state["ffd"] в одном тесте
    # упрётся в закэшированное значение TTL следующего теста.
    kassa_app.SERVICE_CACHE["value"] = None
    kassa_app.SERVICE_CACHE["at"] = 0.0
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0
    kassa_app.FFD_STATE["value"] = None
    kassa_app.FFD_STATE["at"] = None
    # Кэш модели ККТ (MODEL_STATE) — та же история, что у FFD_STATE: иначе
    # переключение demo.DemoKKT.state["model"] в одном тесте упрётся
    # в закэшированное значение TTL следующего теста.
    kassa_app.MODEL_STATE["value"] = None
    kassa_app.MODEL_STATE["at"] = None
    # Кэш структуры таблиц (/api/tables) — та же история, что у MODEL_STATE.
    kassa_app.TABLES_CACHE["value"] = None
    kassa_app.TABLES_CACHE["at"] = None
    demo.DemoKKT.state.update(
        shift_open=False, shift_number=6, receipt_number=0,
        last_fd=24, receipt_open=False,
        clock_offset=-268.0, date_pending=None, last_document_at=None,
        ffd=2, model=250,
    )
    demo.DemoKKT.state["ops"] = []
    demo.DemoKKT.state["docs"] = []
    # base_url важен: сервер принимает только локальное имя хоста,
    # а TestClient по умолчанию представляется как «testserver».
    return TestClient(kassa_app.app, base_url="http://127.0.0.1")


def test_страница_отдаётся(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Касса" in r.text


def test_страница_содержит_журнал_событий(client):
    """Грубая защита от случайного удаления панели журнала при будущих правках."""
    r = client.get("/")
    assert 'id="journal"' in r.text
    assert "kassa.journal" in r.text


def test_запрос_с_чужим_именем_хоста_отбивается(client):
    r = client.get("/api/config", headers={"Host": "attacker.example.com"})
    assert r.status_code == 403


def test_статус_показывает_закрытую_смену(client):
    st = client.get("/api/status").json()
    assert st["online"] is True
    assert st["demo"] is True
    assert st["shift_open"] is False


def test_статус_отдаёт_числовой_код_режима_согласованный_со_строкой(client):
    st = client.get("/api/status").json()
    assert st["mode_code"] == 4
    assert st["mode"] == "Смена закрыта"


def test_чек_без_позиций_не_уходит_в_кассу(client):
    r = client.post("/api/receipt", json={"positions": [], "cash": 0})
    assert r.status_code == 400
    assert "ни одной позиции" in r.json()["detail"]


def test_позиция_без_наименования_не_уходит_в_кассу(client):
    r = client.post("/api/receipt", json={
        "positions": [{"name": "   ", "qty": 1, "price": 10}], "cash": 10})
    assert r.status_code == 400
    assert "наименование" in r.json()["detail"]


def test_нецифровой_фпд_не_уходит_в_кассу(client):
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 10}], "cash": 10,
        "corrected_fpd": "20А7414"})
    assert r.status_code == 400
    assert "цифр" in r.json()["detail"]
    assert demo.DemoKKT.state["receipt_open"] is False


def test_чек_с_цифровым_фпд_пробивается(client):
    """
    Валидный corrected_fpd доходит до k.send_tlv() — в демо-режиме это
    DemoKKT.send_tlv(). Без него запрос падал бы AttributeError'ом.
    """
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 10}], "cash": 10,
        "corrected_fpd": "2074148893"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["fd_number"]
    assert demo.DemoKKT.state["receipt_open"] is False


def test_недоплата_отбивается_до_открытия_чека(client):
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 100}], "cash": 50})
    assert r.status_code == 400
    assert "меньше суммы чека" in r.json()["detail"]
    # чек не открывался — касса осталась чистой
    assert demo.DemoKKT.state["receipt_open"] is False


def test_сдача_с_электронных_отбивается(client):
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 100}], "electronic": 500})
    assert r.status_code == 400
    assert "сдачи" in r.json()["detail"]


def test_чек_при_закрытой_смене_отбивает_касса(client):
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 1}], "cash": 1})
    assert r.status_code == 400
    assert "Смена не открыта" in r.json()["detail"]


def test_удачный_чек_двигает_номер_фд(client):
    client.post("/api/shift/open")
    before = client.get("/api/status").json()["last_fd"]
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка яхты", "qty": 2, "price": 150.5}], "cash": 301})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["fd_number"] == before + 1
    assert demo.DemoKKT.state["receipt_open"] is False


def test_упавшая_позиция_не_оставляет_открытый_чек(client, monkeypatch):
    """Если позиция не прошла, чек обязан быть аннулирован, а не повиснуть."""
    client.post("/api/shift/open")

    def падает(self, **kw):
        raise kassa_app.shtrih.KKTError(0x03, "Неверная длина команды")

    monkeypatch.setattr(demo.DemoKKT, "operation", падает)
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 1}], "cash": 1})
    assert r.status_code == 400
    assert demo.DemoKKT.state["receipt_open"] is False


def test_коррекция_без_основания_не_уходит(client):
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "total": 100, "cash": 100, "reason_description": "  "})
    assert r.status_code == 400
    assert "основания" in r.json()["detail"]


def test_коррекция_с_несходящимися_суммами_не_уходит(client):
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "total": 100, "cash": 50, "reason_description": "не пробит чек"})
    assert r.status_code == 400
    assert "не сходятся" in r.json()["detail"]


def test_коррекция_с_возвратом_прихода_не_уходит(client):
    """В ФФД 1.05 у чека коррекции допустимы только приход и расход."""
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "op_type": 2, "total": 100, "cash": 100,
        "reason_description": "не пробит чек за стоянку"})
    assert r.status_code == 400
    assert "приход и расход" in r.json()["detail"]
    assert demo.DemoKKT.state["receipt_open"] is False


def test_удачная_коррекция_возвращает_фискальный_признак(client):
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "total": 100, "cash": 100,
        "reason_description": "не пробит чек за стоянку",
        "reason_date": "2026-08-20", "reason_number": "б/н"})
    assert r.status_code == 200
    assert r.json()["fiscal_sign"] > 0


def test_настройки_сохраняются_и_читаются(client):
    r = client.post("/api/config", json={
        "host": "10.0.0.5", "port": 7778,
        "operator_password": 11, "admin_password": 22, "tax_system": "osn"})
    assert r.status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["host"] == "10.0.0.5"
    assert cfg["operator_password"] == 11
    assert cfg["admin_password"] == 22
    assert cfg["tax_system"] == "osn"


def test_настройки_с_неизвестной_сно_не_сохраняются(client):
    r = client.post("/api/config", json={"host": "10.0.0.5", "tax_system": "нет такой"})
    assert r.status_code == 400


def test_журнал_обмена_доступен(client):
    client.post("/api/shift/open")
    lines = client.get("/api/log").json()["lines"]
    assert any("смена" in line for line in lines)


def test_чек_на_нулевую_сумму_не_уходит(client):
    """Нулевой чек — почти всегда незаполненная форма, а не расчёт на ноль."""
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 0}], "cash": 0})
    assert r.status_code == 400
    assert "ноль" in r.json()["detail"]
    assert demo.DemoKKT.state["receipt_open"] is False


def test_ping_опознаёт_наш_сервер(client):
    body = client.get("/api/ping").json()
    assert body["app"] == "kassa"
    assert body["pid"] > 0


def test_пустой_адрес_кассы_не_сохраняется(client):
    r = client.post("/api/config", json={"host": "   "})
    assert r.status_code == 400
    assert "пустым" in r.json()["detail"]


def test_конфиг_создаётся_при_первом_запуске(tmp_path, monkeypatch):
    """Адрес кассы должен жить в config.json, а не в коде."""
    target = tmp_path / "config.json"
    monkeypatch.setattr(kassa_app, "CONFIG_PATH", target)
    monkeypatch.setattr(kassa_app, "BASE", tmp_path)
    (tmp_path / "config.example.json").write_text(
        '{"host": "10.1.2.3", "port": 7778}', encoding="utf-8")

    assert not target.exists()
    kassa_app.ensure_config()
    assert target.exists()
    assert kassa_app.load_config()["host"] == "10.1.2.3"
    assert oct(target.stat().st_mode)[-3:] == "600"   # там пароли кассы


def test_статус_без_адреса_кассы_объясняет_причину(tmp_path, monkeypatch):
    monkeypatch.setattr(kassa_app, "DEMO", False)
    monkeypatch.setattr(kassa_app, "CONFIG_PATH", tmp_path / "config.json")
    kassa_app.STATUS_CACHE["value"] = None
    kassa_app.STATUS_CACHE["at"] = 0.0
    c = TestClient(kassa_app.app, base_url="http://127.0.0.1")
    st = c.get("/api/status").json()
    assert st["online"] is False
    assert st["no_host"] is True


def test_выключение_останавливает_сервер(client, monkeypatch):
    stopped = []
    monkeypatch.setattr(kassa_app, "terminate", lambda: stopped.append(True))
    r = client.post("/api/quit")
    assert r.status_code == 200
    time.sleep(0.6)                      # гасим с задержкой, чтобы ответ дошёл
    assert stopped == [True]


def test_выключение_не_обрывает_обмен_с_кассой(client):
    """Пока идёт печать, сервер гасить нельзя: чек останется открытым."""
    assert kassa_app.KKT_LOCK.acquire(blocking=False)
    try:
        r = client.post("/api/quit")
        assert r.status_code == 409
        assert "занята" in r.json()["detail"]
    finally:
        kassa_app.KKT_LOCK.release()


def test_панель_обслуживания_работает_на_эмуляторе(client):
    r = client.get("/api/service")
    assert r.status_code == 200
    body = r.json()
    assert body["online"] is True
    assert body["fn_expiry_warning"] is False
    assert body["unconfirmed_warning"] is False
    assert body["registrations"] == 1         # в архиве ФН один отчёт — регистрация
    assert body["reregistrations"] == 0      # перерегистраций ещё не было
    assert body["fp_counters"] is None       # эмулятор отдаёт нули — на экран не выводим
    assert body["license"] == "0100000000"   # 5 байт эмулятора hex в верхнем регистре


def test_панель_обслуживания_переживает_отказ_кассы_на_чтении_лицензии(client, monkeypatch):
    """1Dh — команда за пределами спецификации: эта прошивка на неизвестные
    команды отвечает 0x37, как уже было с FF60h и FF63h. Отказ на ней не
    должен ронять всю панель обслуживания — только license становится None."""
    def отказ(self):
        raise kassa_app.shtrih.KKTError(0x37, "Команда не поддерживается")

    monkeypatch.setattr(demo.DemoKKT, "read_license", отказ)
    r = client.get("/api/service")
    assert r.status_code == 200
    body = r.json()
    assert body["online"] is True
    assert body["license"] is None
    assert "0x37" in body["license_error"]
    assert body["registrations"] == 1        # остальные поля панели на месте


def test_панель_обслуживания_не_выдаёт_чужой_отказ_за_неподдержку(client, monkeypatch):
    """Живая касса отказала на 1Dh — но выдавать любой отказ за 0x37 нельзя:
    причина должна приходить с кассы, а не додумываться панелью."""
    def отказ(self):
        raise kassa_app.shtrih.KKTError(0x05, "Неверный пароль")

    monkeypatch.setattr(demo.DemoKKT, "read_license", отказ)
    body = client.get("/api/service").json()
    assert body["license"] is None
    assert "0x05" in body["license_error"]
    assert "не поддерживается" not in body["license_error"]


def test_панель_обслуживания_переживает_короткую_занятость(client):
    """
    Гонка при загрузке страницы: опрос статуса на мгновение забирает замок
    раньше панели обслуживания. Панель должна дождаться, а не сдаться сразу.
    """
    kassa_app.SERVICE_CACHE["value"] = None
    kassa_app.SERVICE_CACHE["at"] = 0.0

    def hold():
        kassa_app.KKT_LOCK.acquire()
        time.sleep(0.3)
        kassa_app.KKT_LOCK.release()

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.05)                     # дать потоку захватить замок первым
    try:
        r = client.get("/api/service")
    finally:
        t.join()
    assert r.status_code == 200
    assert r.json()["online"] is True


def test_панель_обслуживания_честно_отказывает_при_долгой_занятости(client, monkeypatch):
    """Печатающая операция (до 90 секунд) — панель не висит, а сдаётся."""
    monkeypatch.setattr(kassa_app, "SERVICE_WAIT", 0.2)
    kassa_app.SERVICE_CACHE["value"] = None
    kassa_app.SERVICE_CACHE["at"] = 0.0

    def hold():
        kassa_app.KKT_LOCK.acquire()
        try:
            time.sleep(1.0)
        finally:
            kassa_app.KKT_LOCK.release()

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.05)                     # дать потоку захватить замок первым
    try:
        r = client.get("/api/service")
    finally:
        t.join()
    assert r.status_code == 200
    body = r.json()
    assert body["busy"] is True
    assert body["online"] is False
    assert "clock_drift_rate" in body
    assert "clock_drift_rate_days" in body
    assert "clock_drift_rate_points" in body


# --- Сверка часов (21h/22h/23h) -------------------------------------------

def test_сверка_времени_убирает_уход_часов(client):
    r = client.post("/api/clock/time")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert abs(body["drift_seconds"] - (-268.0)) < 1.0
    assert abs(demo.DemoKKT.state["clock_offset"]) < 1.0


def test_сверка_времени_отказывает_при_открытой_смене(client):
    client.post("/api/shift/open")
    r = client.post("/api/clock/time")
    assert r.status_code == 400
    assert "смен" in r.json()["detail"].lower()


def test_сверка_времени_отказывает_при_открытом_чеке(client):
    demo.DemoKKT.state["receipt_open"] = True
    r = client.post("/api/clock/time")
    assert r.status_code == 400
    assert "чек" in r.json()["detail"].lower()


def test_сверка_времени_отказывает_если_время_раньше_последнего_фд(client):
    from datetime import datetime, timedelta
    demo.DemoKKT.state["last_document_at"] = datetime.now() + timedelta(days=1)
    r = client.post("/api/clock/time")
    assert r.status_code == 400
    assert "документ" in r.json()["detail"].lower()


def test_сверка_времени_отказывает_при_расхождении_больше_суток(client):
    demo.DemoKKT.state["clock_offset"] = -2 * 24 * 3600
    r = client.post("/api/clock/time")
    assert r.status_code == 400
    assert "суток" in r.json()["detail"].lower()


def test_сверка_даты_приводит_дату_кассы_к_дате_компьютера(client):
    from datetime import date
    demo.DemoKKT.state["clock_offset"] = -120.0
    r = client.post("/api/clock/date")
    assert r.status_code == 200
    assert demo.DemoKKT.state["date_pending"] is None
    st = client.get("/api/status").json()
    assert st["mode"] != "Ожидание подтверждения даты"
    assert st["datetime"].split(" ")[0] == date.today().strftime("%d.%m.%Y")


def test_из_режима_6_время_отказывает_а_дата_проходит(client):
    from datetime import date
    demo.DemoKKT.state["date_pending"] = date.today()
    r1 = client.post("/api/clock/time")
    assert r1.status_code == 400
    assert "дат" in r1.json()["detail"].lower()

    r2 = client.post("/api/clock/date")
    assert r2.status_code == 200
    assert demo.DemoKKT.state["date_pending"] is None


def test_сверка_времени_отказывает_на_нечитаемых_часах_кассы(client, monkeypatch):
    """Севшая батарейка кассы возвращает «00.00.0000» вместо даты — сверка не
    должна ронять питоновским «time data does not match format», владелец
    должен увидеть русское объяснение."""
    real_long_status = demo.DemoKKT.long_status

    def сломанный_long_status(self):
        d = real_long_status(self)
        d["date"] = "00.00.0000"
        return d

    monkeypatch.setattr(demo.DemoKKT, "long_status", сломанный_long_status)
    r = client.post("/api/clock/time")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "time data" not in detail
    assert "час" in detail.lower()


def test_days_left_обычный_случай():
    from datetime import date
    assert kassa_app.days_left("20.08.2026", today=date(2026, 8, 10)) == 10


def test_days_left_срок_в_прошлом_отрицательное_число():
    from datetime import date
    assert kassa_app.days_left("01.01.2020", today=date(2026, 8, 24)) < 0


def test_days_left_неразбираемая_строка_даёт_none():
    assert kassa_app.days_left("не дата") is None


def test_остановка_не_трогает_чужой_сервис(capsys):
    """--stop гасит только нашу кассу, опознав её по /api/ping."""
    free = kassa_app.free_port(9700)
    assert kassa_app.stop_instance(free) == 0
    assert "никто не слушает" in capsys.readouterr().out


# --- Журнал ухода часов ----------------------------------------------------

def test_первый_статус_дописывает_замер(client):
    client.get("/api/status")
    lines = kassa_app.CLOCK_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert {"at", "kkt", "drift"} <= entry.keys()
    assert entry["drift"] < 0
    assert abs(entry["drift"] - (-268.0)) < 5.0


def test_второй_статус_подряд_не_добавляет_строку(client):
    client.get("/api/status")
    client.get("/api/status")
    lines = kassa_app.CLOCK_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_успешная_сверка_дописывает_отметку_sync(client):
    r = client.post("/api/clock/time")
    assert r.status_code == 200
    lines = kassa_app.CLOCK_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "sync"
    assert abs(entry["drift"] - r.json()["drift_seconds"]) < 0.001


def test_скорость_ухода_считается_по_отрезку_после_сверки():
    from datetime import datetime, timedelta
    base = datetime(2026, 1, 1)
    entries = [
        {"at": base.isoformat(), "kkt": base.isoformat(), "drift": -1000.0},
        {"at": (base + timedelta(days=1)).isoformat(), "kkt": "", "drift": -1010.0,
         "kind": "sync"},
        {"at": (base + timedelta(days=2)).isoformat(), "drift": -1.0},
        {"at": (base + timedelta(days=5)).isoformat(), "drift": -3.0},
        {"at": (base + timedelta(days=10)).isoformat(), "drift": -5.0},
    ]
    result = kassa_app.drift_rate(entries)
    assert result is not None
    assert result["points"] == 3
    assert abs(result["days"] - 8.0) < 1e-9
    assert abs(result["rate"] - ((-5.0 - (-1.0)) / 8.0)) < 1e-9


def test_скорость_ухода_none_при_нехватке_данных():
    from datetime import datetime, timedelta
    base = datetime(2026, 1, 1)
    # точек меньше трёх, хотя плечо больше 7 суток
    мало_точек = [
        {"at": base.isoformat(), "drift": -1.0},
        {"at": (base + timedelta(days=10)).isoformat(), "drift": -5.0},
    ]
    assert kassa_app.drift_rate(мало_точек) is None

    # точек достаточно, но плечо меньше 7 суток
    короткое_плечо = [
        {"at": base.isoformat(), "drift": -1.0},
        {"at": (base + timedelta(days=1)).isoformat(), "drift": -2.0},
        {"at": (base + timedelta(days=2)).isoformat(), "drift": -3.0},
    ]
    assert kassa_app.drift_rate(короткое_плечо) is None


def test_скорость_ухода_не_падает_на_мусоре():
    from datetime import datetime, timedelta
    base = datetime(2026, 1, 1)
    entries = [
        "не словарь",
        {"at": "не дата", "drift": -1.0},
        {"drift": -1.0},                       # нет at
        {"at": base.isoformat()},               # нет drift
        {"at": base.isoformat(), "drift": -1.0},
        {"at": (base + timedelta(days=4)).isoformat(), "drift": -2.0},
        {"at": (base + timedelta(days=8)).isoformat(), "drift": -4.0},
    ]
    result = kassa_app.drift_rate(entries)
    assert result is not None
    assert result["points"] == 3


def test_недоступный_журнал_не_ломает_статус(client, tmp_path, monkeypatch):
    """Каталог вместо файла журнала не должен ронять /api/status."""
    bad = tmp_path / "clock_dir.log"
    bad.mkdir()
    monkeypatch.setattr(kassa_app, "CLOCK_LOG", bad)
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["online"] is True


# --- Устаревший сервер (версия кода в памяти vs исходники на диске) -------

def test_ping_на_свежем_коде_не_устарел(client):
    body = client.get("/api/ping").json()
    assert body["stale"] is False
    assert body["version"]


def test_is_stale_true_при_подменённой_версии(client, monkeypatch):
    monkeypatch.setattr(kassa_app, "CODE_VERSION", "заведомо-другая-строка")
    assert kassa_app.is_stale() is True


def test_статус_отдаёт_stale_и_свежим_и_из_кэша(client):
    first = client.get("/api/status").json()
    assert "stale" in first
    assert first["stale"] is False
    second = client.get("/api/status").json()
    assert "stale" in second
    assert second["stale"] is False


def test_устаревший_сервер_отбивает_печать_чека(client, monkeypatch):
    client.post("/api/shift/open")
    monkeypatch.setattr(kassa_app, "CODE_VERSION", "заведомо-другая-строка")
    kassa_app._STALE_CACHE["at"] = 0.0    # открытие смены уже закэшировало is_stale()
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 10}], "cash": 10})
    assert r.status_code == 409
    assert "стар" in r.json()["detail"].lower()


def test_устаревший_сервер_отбивает_открытие_смены_и_сверку_времени(client, monkeypatch):
    monkeypatch.setattr(kassa_app, "CODE_VERSION", "заведомо-другая-строка")
    assert client.post("/api/shift/open").status_code == 409
    assert client.post("/api/clock/time").status_code == 409


def test_устаревший_сервер_не_блокирует_z_отчёт_и_аннулирование(client, monkeypatch):
    """Z-отчёт и аннулирование чека — аварийный выход, доступный всегда."""
    monkeypatch.setattr(kassa_app, "CODE_VERSION", "заведомо-другая-строка")
    assert client.post("/api/shift/close").status_code != 409
    assert client.post("/api/receipt/cancel").status_code != 409


def test_перезапуск_отказывает_при_занятой_кассе(client):
    assert kassa_app.KKT_LOCK.acquire(blocking=False)
    try:
        r = client.post("/api/restart")
        assert r.status_code == 409
        assert "занята" in r.json()["detail"]
    finally:
        kassa_app.KKT_LOCK.release()


def test_перезапуск_на_свободной_кассе_зовёт_restart(client, monkeypatch):
    restarted = []
    monkeypatch.setattr(kassa_app, "restart", lambda: restarted.append(True))
    r = client.post("/api/restart")
    assert r.status_code == 200
    time.sleep(0.6)                      # перезапускаем с задержкой, чтобы ответ дошёл
    assert restarted == [True]


# --- Готовность к ФФД 1.2 ---------------------------------------------------

def test_панель_ффд_работает_на_эмуляторе(client):
    r = client.get("/api/ffd")
    assert r.status_code == 200
    body = r.json()
    assert body["online"] is True
    assert body["current"] == "1.05"
    assert body["fn_max"] == "1.2"
    assert body["kkt_max"] == "1.05"
    assert [c["key"] for c in body["checks"]] == [
        "ffd_current", "fn", "kkt", "fn_expiry", "shift", "ofd", "code",
    ]
    assert body["verdict"]["state"] != "unknown"


def test_панель_обслуживания_содержит_ффд_по_длине(client):
    body = client.get("/api/service").json()
    assert body["ffd_by_length"] == "1.05"


# --- Защёлка на версию ФФД перед печатью -----------------------------------

def test_ффд_1_05_печатает_чек_и_коррекцию_как_раньше(client):
    """Baseline: при ffd = 2 (1.05) защёлка не мешает — программа умеет 1.05."""
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 10}], "cash": 10})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.post("/api/correction", json={
        "total": 100, "cash": 100, "reason_description": "не пробит чек"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_ффд_1_2_печатает_чек(client):
    """Ветка кассового чека под 1.2 есть и печатает."""
    demo.DemoKKT.state["ffd"] = 4
    client.post("/api/shift/open")

    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 10}], "cash": 10})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_ффд_1_2_отправляет_тег_2108_после_каждой_позиции(client):
    """На этой модели (250, ШТРИХ-М-02Ф) FF4Dh с тегом 2108 уходит после FF46h."""
    demo.DemoKKT.state["ffd"] = 4
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [
            {"name": "Стоянка", "qty": 1, "price": 10},
            {"name": "Мойка", "qty": 2, "price": 5},
        ],
        "cash": 20,
    })
    assert r.status_code == 200
    ops = demo.DemoKKT.state["ops"]
    assert [kind for kind, _ in ops] == ["operation", "tlv", "operation", "tlv"]
    for kind, payload in ops:
        if kind == "tlv":
            tag = int.from_bytes(payload[0:2], "little")
            assert tag == 2108


def test_ффд_1_2_на_штрих_мобайл_отправляет_тег_2108_перед_позицией(client):
    """На модели 19 (ШТРИХ-МОБАЙЛ-Ф) порядок обратный: FF4Dh перед FF46h."""
    demo.DemoKKT.state["ffd"] = 4
    demo.DemoKKT.state["model"] = 19
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [
            {"name": "Стоянка", "qty": 1, "price": 10},
            {"name": "Мойка", "qty": 2, "price": 5},
        ],
        "cash": 20,
    })
    assert r.status_code == 200
    ops = demo.DemoKKT.state["ops"]
    assert [kind for kind, _ in ops] == ["tlv", "operation", "tlv", "operation"]


def test_ффд_1_05_не_отправляет_тег_2108(client):
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 10}], "cash": 10})
    assert r.status_code == 200
    assert not any(kind == "tlv" for kind, _ in demo.DemoKKT.state["ops"])


def test_ффд_1_1_отбивает_и_чек_и_коррекцию_не_двигая_номер_фд(client):
    """1.1 — версия, для которой ветки нет ни у чека, ни у коррекции."""
    demo.DemoKKT.state["ffd"] = 3
    client.post("/api/shift/open")
    last_fd_before = demo.DemoKKT.state["last_fd"]

    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 10}], "cash": 10})
    assert r.status_code == 409
    assert "1.1" in r.json()["detail"]

    r = client.post("/api/correction", json={
        "total": 100, "cash": 100, "reason_description": "не пробит чек"})
    assert r.status_code == 409
    assert "1.1" in r.json()["detail"]

    assert demo.DemoKKT.state["last_fd"] == last_fd_before
    assert demo.DemoKKT.state["docs"] == []


# --- Чек коррекции ФФД 1.2 (обычный чек с позициями) ----------------------

def test_ффд_1_2_коррекция_без_позиций_не_уходит(client):
    demo.DemoKKT.state["ffd"] = 4
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "total": 100, "cash": 100, "reason_description": "не пробит чек"})
    assert r.status_code == 400
    assert "позици" in r.json()["detail"].lower()
    assert demo.DemoKKT.state["docs"] == []


def test_ффд_1_2_удачная_коррекция_открывает_документ_типом_0x80(client):
    demo.DemoKKT.state["ffd"] = 4
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "total": 100, "cash": 100,
        "reason_description": "не пробит чек за стоянку",
        "reason_date": "2026-08-20", "reason_number": "б/н",
        "positions": [{"name": "Стоянка", "qty": 1, "price": 100}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["fd_number"] > 0
    assert body["fiscal_sign"] > 0

    docs = demo.DemoKKT.state["docs"]
    assert docs[0] == ("open", shtrih.DOC_CORRECTION_FLAG | shtrih.DOC_SALE)
    assert [kind for kind, _ in docs[1:3]] == ["doc_tlv", "doc_tlv"]
    assert docs[-1] == ("close", None)

    ops = demo.DemoKKT.state["ops"]
    assert [kind for kind, _ in ops] == ["operation", "tlv"]


def test_ффд_1_2_коррекция_возврат_прихода_проходит_а_на_1_05_нет(client):
    demo.DemoKKT.state["ffd"] = 4
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "op_type": shtrih.OP_INCOME_RETURN, "total": 100, "cash": 100,
        "reason_description": "не пробит чек за стоянку",
        "positions": [{"name": "Стоянка", "qty": 1, "price": 100}],
    })
    assert r.status_code == 200
    docs = demo.DemoKKT.state["docs"]
    assert docs[0] == ("open", shtrih.DOC_CORRECTION_FLAG | shtrih.DOC_SALE_RETURN)

    kassa_app.FFD_STATE["value"] = None
    kassa_app.FFD_STATE["at"] = None
    demo.DemoKKT.state["ffd"] = 2
    r = client.post("/api/correction", json={
        "op_type": shtrih.OP_INCOME_RETURN, "total": 100, "cash": 100,
        "reason_description": "не пробит чек за стоянку",
        "positions": [{"name": "Стоянка", "qty": 1, "price": 100}],
    })
    assert r.status_code == 400
    assert "приход и расход" in r.json()["detail"]


def test_ффд_1_2_коррекция_по_предписанию_без_номера_не_уходит(client):
    demo.DemoKKT.state["ffd"] = 4
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "correction_type": 1, "total": 100, "cash": 100,
        "reason_description": "не пробит чек за стоянку",
        "positions": [{"name": "Стоянка", "qty": 1, "price": 100}],
    })
    assert r.status_code == 400
    assert "предписан" in r.json()["detail"].lower()


def test_ффд_1_2_коррекция_не_передаёт_тег_1177_в_основании(client):
    demo.DemoKKT.state["ffd"] = 4
    client.post("/api/shift/open")
    r = client.post("/api/correction", json={
        "total": 100, "cash": 100,
        "reason_description": "не пробит чек за стоянку",
        "reason_date": "2026-08-20", "reason_number": "б/н",
        "positions": [{"name": "Стоянка", "qty": 1, "price": 100}],
    })
    assert r.status_code == 200

    reason_tlv = None
    for kind, payload in demo.DemoKKT.state["docs"]:
        if kind == "doc_tlv" and int.from_bytes(payload[0:2], "little") == 1174:
            reason_tlv = payload
    assert reason_tlv is not None

    inner = reason_tlv[4:]
    tags = []
    i = 0
    while i < len(inner):
        t = int.from_bytes(inner[i:i + 2], "little")
        n = int.from_bytes(inner[i + 2:i + 4], "little")
        tags.append(t)
        i += 4 + n
    assert 1177 not in tags
    assert tags == [1178, 1179]


def test_api_ffd_отдаёт_code_ffd_со_списком_версий_и_карточку_code_ok(client):
    body = client.get("/api/ffd").json()
    assert "1.05" in body["code_ffd"]
    assert "1.2" in body["code_ffd"]
    code_card = next(c for c in body["checks"] if c["key"] == "code")
    assert code_card["state"] == "ok"


def test_api_ffd_отдаёт_чек_лист_готовности_программы(client):
    body = client.get("/api/ffd").json()
    steps = body["steps"]
    assert [s["key"] for s in steps] == [
        "registry", "code", "subscription", "keys", "driver", "shift", "ofd",
        "firmware", "settlement", "reregister", "table19", "lk_fns", "trial",
    ]


def test_api_ffd_written_у_correction_выводится_из_correction_ffd_а_не_вписан(
    client, monkeypatch
):
    monkeypatch.setattr(kassa_app, "CORRECTION_FFD", ("1.05",))
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0
    body = client.get("/api/ffd").json()
    code_step = next(s for s in body["steps"] if s["key"] == "code")
    assert code_step["state"] == "todo"
    code_card = next(c for c in body["checks"] if c["key"] == "code")
    assert "чек коррекции" not in code_card["value"]
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0


def test_api_ffd_карточка_code_упоминает_чек_и_коррекцию_на_текущих_кортежах(client):
    body = client.get("/api/ffd").json()
    code_card = next(c for c in body["checks"] if c["key"] == "code")
    assert "кассовый чек" in code_card["value"]
    assert "чек коррекции" in code_card["value"]
    assert code_card["state"] == "ok"


def test_api_ffd_на_1_05_lk_fns_и_firmware_todo_code_done(client):
    demo.DemoKKT.state["ffd"] = 2
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0
    body = client.get("/api/ffd").json()
    steps = {s["key"]: s for s in body["steps"]}
    assert steps["lk_fns"]["state"] == "todo"
    assert steps["firmware"]["state"] == "todo"
    assert steps["code"]["state"] == "done"


def test_api_ffd_на_1_2_lk_fns_и_firmware_done(client):
    demo.DemoKKT.state["ffd"] = 4
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0
    body = client.get("/api/ffd").json()
    steps = {s["key"]: s for s in body["steps"]}
    assert steps["lk_fns"]["state"] == "done"
    # Касса, работающая по 1.2, обязана и уметь 1.2: эмулятор до этой правки
    # изображал невозможное — тег 1209 «1.2» при теге 1189 «1.05».
    assert steps["firmware"]["state"] == "done"


def test_api_ffd_смена_открыта_todo_закрыта_done(client):
    demo.DemoKKT.state["shift_open"] = True
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0
    body = client.get("/api/ffd").json()
    shift_step = next(s for s in body["steps"] if s["key"] == "shift")
    assert shift_step["state"] == "todo"

    demo.DemoKKT.state["shift_open"] = False
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0
    body = client.get("/api/ffd").json()
    shift_step = next(s for s in body["steps"] if s["key"] == "shift")
    assert shift_step["state"] == "done"


def test_api_ffd_subscription_settlement_reregister_trial_всегда_manual(client):
    for ffd_code in (2, 4):
        demo.DemoKKT.state["ffd"] = ffd_code
        kassa_app.FFD_CACHE["value"] = None
        kassa_app.FFD_CACHE["at"] = 0.0
        body = client.get("/api/ffd").json()
        steps = {s["key"]: s for s in body["steps"]}
        assert steps["subscription"]["state"] == "manual"
        assert steps["settlement"]["state"] == "manual"
        assert steps["reregister"]["state"] == "manual"
        assert steps["trial"]["state"] == "manual"


def test_api_ffd_keys_на_эмуляторе_todo_без_ключей(client):
    """Эмулятор держит в таблице 23 поле 11 прочерки — как касса без ключей."""
    body = client.get("/api/ffd").json()
    keys_step = next(s for s in body["steps"] if s["key"] == "keys")
    assert keys_step["state"] == "todo"
    assert "без ключей" in keys_step["note"]


def test_api_ffd_keys_done_если_uin_не_пуст(client, monkeypatch):
    monkeypatch.setitem(demo.DEMO_TABLES[23]["fields"][11], "value", "10000001")
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0
    body = client.get("/api/ffd").json()
    keys_step = next(s for s in body["steps"] if s["key"] == "keys")
    assert keys_step["state"] == "done"


def test_api_ffd_keys_unknown_при_отказе_кассы_панель_не_падает(client, monkeypatch):
    def отказ(self, table, row, field):
        raise kassa_app.shtrih.KKTError(0x50, "Неверный номер таблицы")

    monkeypatch.setattr(demo.DemoKKT, "read_table", отказ)
    r = client.get("/api/ffd")
    assert r.status_code == 200
    body = r.json()
    keys_step = next(s for s in body["steps"] if s["key"] == "keys")
    assert keys_step["state"] == "unknown"
    assert [s["key"] for s in body["steps"]] == [
        "registry", "code", "subscription", "keys", "driver", "shift", "ofd",
        "firmware", "settlement", "reregister", "table19", "lk_fns", "trial",
    ]


def test_api_ffd_reregister_называет_причину_4_а_не_7(client):
    body = client.get("/api/ffd").json()
    reregister_step = next(s for s in body["steps"] if s["key"] == "reregister")
    assert "4 «Изменение настроек ККТ»" in reregister_step["note"]
    assert "7 «изменение настроек ККТ»" not in reregister_step["note"]


def test_api_ffd_отдаёт_vat_5_7_written_и_не_отдаёт_program(client):
    body = client.get("/api/ffd").json()
    assert body["vat_5_7_written"] is False
    assert "program" not in body


def test_api_ffd_модель_разрешена_фнс_со_ссылкой_на_приказ(client):
    """Строка про реестр закрыта приказом ФНС, а не ждёт проверки владельцем."""
    body = client.get("/api/ffd").json()
    registry = next(s for s in body["steps"] if s["key"] == "registry")
    assert registry["state"] == "done"
    assert "АБ-7-20/782@" in registry["note"]


def test_api_ffd_settlement_идёт_после_firmware_в_списке_шагов(client):
    body = client.get("/api/ffd").json()
    keys = [s["key"] for s in body["steps"]]
    assert keys.index("settlement") > keys.index("firmware")


def test_api_ffd_отдаёт_vat_22_written_false_на_текущем_справочнике(client):
    body = client.get("/api/ffd").json()
    assert body["vat_22_written"] is False


def test_api_ffd_vat_22_written_выводится_из_справочника_а_не_вписан(
    client, monkeypatch
):
    # Значение байта выдумано: тесту важно только наличие ключа. Настоящий
    # код ставки 22% берётся из спецификации, когда её будут добавлять.
    monkeypatch.setitem(shtrih.VAT_RATES, "22", 0x8C)
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0
    body = client.get("/api/ffd").json()
    assert body["vat_22_written"] is True
    kassa_app.FFD_CACHE["value"] = None
    kassa_app.FFD_CACHE["at"] = 0.0


def test_ффд_1_2_не_блокирует_смену_и_х_отчёт_и_аннулирование(client):
    demo.DemoKKT.state["ffd"] = 4
    assert client.post("/api/shift/open").status_code == 200
    assert client.post("/api/report/x").status_code == 200
    assert client.post("/api/receipt/cancel").status_code == 200
    assert client.post("/api/shift/close").status_code == 200


def test_отчёт_о_состоянии_расчётов_отдаёт_фд_и_фискальный_признак(client):
    r = client.post("/api/report/settlement")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["fd_number"]
    assert body["fiscal_sign"]


def test_неизвестная_ффд_не_блокирует_печать(client):
    """Версию определить не удалось вовсе — печатаем как раньше (решение владельца)."""
    demo.DemoKKT.state["ffd"] = None
    client.post("/api/shift/open")
    r = client.post("/api/receipt", json={
        "positions": [{"name": "Стоянка", "qty": 1, "price": 10}], "cash": 10})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_service_отдаёт_ffd_blocked(client):
    # 1.05 (код 2) — ветка есть, плашки нет.
    demo.DemoKKT.state["ffd"] = 2
    kassa_app.SERVICE_CACHE["value"] = None
    kassa_app.SERVICE_CACHE["at"] = 0.0
    kassa_app.FFD_STATE["value"] = None
    kassa_app.FFD_STATE["at"] = None
    assert client.get("/api/service").json()["ffd_blocked"] is False

    # 1.2 (код 4) — ветка есть, плашки нет: по точной версии из тега 1209,
    # а не по грубой прикидке «1.1/1.2» из резерва.
    demo.DemoKKT.state["ffd"] = 4
    kassa_app.SERVICE_CACHE["value"] = None
    kassa_app.SERVICE_CACHE["at"] = 0.0
    kassa_app.FFD_STATE["value"] = None
    kassa_app.FFD_STATE["at"] = None
    assert client.get("/api/service").json()["ffd_blocked"] is False

    # 1.1 (код 3) — ветки нет, плашка есть.
    demo.DemoKKT.state["ffd"] = 3
    kassa_app.SERVICE_CACHE["value"] = None
    kassa_app.SERVICE_CACHE["at"] = 0.0
    kassa_app.FFD_STATE["value"] = None
    kassa_app.FFD_STATE["at"] = None
    assert client.get("/api/service").json()["ffd_blocked"] is True


def test_service_не_блокирует_печать_когда_ффд_не_определяется(client):
    """Версию определить не удалось вовсе — печатаем как раньше (решение
    владельца), поэтому и плашка панели обслуживания не загорается."""
    demo.DemoKKT.state["ffd"] = None
    kassa_app.SERVICE_CACHE["value"] = None
    kassa_app.SERVICE_CACHE["at"] = 0.0
    kassa_app.FFD_STATE["value"] = None
    kassa_app.FFD_STATE["at"] = None
    r = client.get("/api/service")
    assert r.status_code == 200
    assert r.json()["ffd_blocked"] is False


def test_api_status_отдаёт_ffd_согласованный_с_состоянием_кассы(client):
    """Форма чека коррекции в интерфейсе берёт версию ФФД отсюда же, откуда
    защёлка перед печатью — источник должен быть один и тот же."""
    demo.DemoKKT.state["ffd"] = 2
    assert client.get("/api/status").json()["ffd"] == "1.05"

    kassa_app.STATUS_CACHE["value"] = None
    kassa_app.STATUS_CACHE["at"] = 0.0
    kassa_app.FFD_STATE["value"] = None
    kassa_app.FFD_STATE["at"] = None
    demo.DemoKKT.state["ffd"] = 4
    assert client.get("/api/status").json()["ffd"] == "1.2"


def test_api_status_не_падает_когда_версия_ффд_не_определяется(client):
    """state["ffd"] = None — как касса, не поддерживающая FF0Eh (и резервный
    путь по длине FF09h тоже не даёт ответа): /api/status не должен падать."""
    demo.DemoKKT.state["ffd"] = None
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["ffd"] is None


# --- Таблицы ККТ (/api/tables) ---------------------------------------------

def test_список_таблиц_отдаёт_таблицы_1_19_и_23(client):
    r = client.get("/api/tables")
    assert r.status_code == 200
    body = r.json()
    assert body["online"] is True
    numbers = {t["number"] for t in body["tables"]}
    assert {1, 19, 23} <= numbers
    table19 = next(t for t in body["tables"] if t["number"] == 19)
    assert table19["name"] == "Параметры ОФД"
    assert table19["rows"] == 1
    assert table19["fields"] == 2


def test_поля_таблицы_19_отдают_char_строкой_bin_числом_и_raw_у_обоих(client):
    r = client.get("/api/tables/19", params={"row": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["number"] == 19
    assert body["row"] == 1
    fields = {f["number"]: f for f in body["fields"]}
    assert fields[1]["type"] == "char"
    assert fields[1]["value"] == "demo.ofd.example"
    assert isinstance(fields[1]["raw"], str) and fields[1]["raw"]
    assert fields[2]["type"] == "bin"
    assert fields[2]["value"] == 7443
    assert isinstance(fields[2]["raw"], str) and fields[2]["raw"]


def test_несуществующая_таблица_не_роняет_сервер(client):
    r = client.get("/api/tables/250")
    assert r.status_code in (400, 404)
