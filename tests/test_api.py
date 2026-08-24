"""
Проверки HTTP-слоя на эмуляторе кассы.

Живая ККТ здесь не нужна и не должна быть нужна: тесты гоняются в демо-режиме,
ничего не печатают и никуда не отправляют.
"""

import time

import pytest
from fastapi.testclient import TestClient

import app as kassa_app
import demo


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(kassa_app, "DEMO", True)
    monkeypatch.setattr(kassa_app, "CONFIG_PATH", tmp_path / "config.json")
    kassa_app.STATUS_CACHE["value"] = None
    kassa_app.STATUS_CACHE["at"] = 0.0
    demo.DemoKKT.state.update(
        shift_open=False, shift_number=6, receipt_number=0,
        last_fd=24, receipt_open=False,
    )
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


def test_остановка_не_трогает_чужой_сервис(capsys):
    """--stop гасит только нашу кассу, опознав её по /api/ping."""
    free = kassa_app.free_port(9700)
    assert kassa_app.stop_instance(free) == 0
    assert "никто не слушает" in capsys.readouterr().out
