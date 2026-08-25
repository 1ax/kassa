"""
Эмулятор ККТ для демо-режима (`python app.py --demo`).

Повторяет публичный интерфейс `shtrih.KKT`, но ничего никуда не шлёт:
держит смену, номера чеков и ФД в памяти процесса. Нужен, чтобы посмотреть
и покликать интерфейс, когда кассы под рукой нет, и чтобы прогонять сценарии
печати, не переводя бумагу и не отправляя документы в ОФД.

Состояние живёт до перезапуска сервера и намеренно не сохраняется на диск:
демо не должно выглядеть как настоящая касса.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import shtrih


class DemoKKT:
    """Заглушка кассы. Интерфейс — как у shtrih.KKT, поведение — правдоподобное."""

    # состояние общее на весь процесс, чтобы переживать отдельные запросы
    state = {
        "shift_open": False,
        "shift_number": 6,
        "receipt_number": 0,
        "last_fd": 24,
        "receipt_open": False,
        "opened_at": None,
        # На сколько секунд эмулируемые часы кассы отстают от компьютера.
        # -268.0 — фактический замер живой кассы на 24.08.2026.
        "clock_offset": -268.0,
        # Дата, ожидающая подтверждения (23h). Не None — касса в режиме 6.
        "date_pending": None,
        # Момент последнего ФД. None — выводим его из эмулируемых часов.
        "last_document_at": None,
        # Версия ФФД (числовой код тега 1209): 2 — 1.05 (как есть у живой
        # кассы на 25.08.2026), 4 — 1.2, None — версия не определяется
        # (касса отвечает 0x37 «команда не поддерживается», как и на живой
        # кассе для FF0Eh). Переключается тестами защёлки перед печатью.
        "ffd": 2,
    }

    def __init__(self, *args, **kwargs):
        self.log: list[str] = []
        self._note("демо-режим: касса эмулируется, ничего не печатается")

    def _note(self, line: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {line}")

    def _now(self) -> datetime:
        """Эмулируемые часы кассы: время компьютера, сдвинутое на clock_offset."""
        return datetime.now() + timedelta(seconds=self.state["clock_offset"])

    def _mode(self) -> int:
        """Режим ККТ: 6 при незавершённой сверке даты, иначе как у настоящей кассы."""
        if self.state["date_pending"] is not None:
            return 6
        if self.state["receipt_open"]:
            return 8
        if self.state["shift_open"]:
            return 2
        return 4

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "DemoKKT":
        return self

    def __exit__(self, *exc) -> None:
        pass

    # -- состояние --

    def device_type(self) -> dict:
        return {"type": 0, "subtype": 0, "protocol": "1.14", "model": 250,
                "language": 0, "name": "ШТРИХ-М-02Ф (демо)"}

    def short_status(self) -> dict:
        mode = self._mode()
        return {
            "operator": 30, "flags": 0x0292, "flags_hex": "0x0292",
            "mode": mode, "mode_name": shtrih.ECR_MODES[mode], "mode_status": 0,
            "submode": 0, "operations_in_receipt": 0, "paper": True,
            "receipt_open": self.state["receipt_open"],
        }

    def long_status(self) -> dict:
        now = self._now()
        mode = self._mode()
        return {
            "operator": 30, "sw_version": "C1", "sw_build": 62922,
            "sw_date": "10.01.2025", "doc_number": self.state["last_fd"] + 1,
            "flags": 0x0292, "mode": mode, "mode_name": shtrih.ECR_MODES[mode],
            "submode": 0, "port": 2,
            "date": now.strftime("%d.%m.%Y"), "time": now.strftime("%H:%M:%S"),
            "serial": 0, "last_closed_shift": self.state["shift_number"] - 1,
            "inn": None,
        }

    def shift_params(self) -> dict:
        return {
            "shift_open": self.state["shift_open"],
            "shift_number": self.state["shift_number"],
            "receipt_number": self.state["receipt_number"],
        }

    def fn_status(self) -> dict:
        return {
            "configured": True, "fiscal_mode_open": True, "fiscal_mode_closed": False,
            "current_document": "кассовый чек" if self.state["receipt_open"]
                                else "нет открытого документа",
            "has_document_data": self.state["receipt_open"],
            "shift_open": self.state["shift_open"], "warnings": 0,
            "last_document_at": (self.state["last_document_at"] or self._now())
                                 .strftime("%d.%m.%Y %H:%M"),
            "fn_number": "0000000000000000", "last_fd": self.state["last_fd"],
        }

    def ofd_status(self) -> dict:
        return {"connected": True, "has_message": False, "waiting_receipt": False,
                "queue_length": 0, "first_document": 0}

    def fn_expiry(self) -> dict:
        year = date.today().year + 2
        return {"expiry": f"01.01.{year}", "tail": "00 00"}

    def fn_version(self) -> dict:
        return {"version": "demo_v_0_0_0", "serial_software": False}

    def fiscalization(self) -> dict:
        # Длина ответа FF09h согласована с state["ffd"] — тот же признак,
        # которым _ffd_by_length() в app.py прикидывает версию ФФД, когда
        # тег 1209 не читается: 48 байт у 1.05, 64 у 1.1/1.2, а для режима
        # «версия не определяется» — заведомо нестандартная длина 99.
        length = {2: 48, 4: 64}.get(self.state["ffd"], 99)
        return {
            "at": "01.01.2000 00:00",
            "inn": "000000000000",
            "reg_number": "0000000000000000",
            "tax_systems": [shtrih.TAX_SYSTEM_NAMES["usn_income"]],
            "work_modes": 0,
            "fd": 1,
            "fp": 111111111,
            "data_length": length,
        }

    def unconfirmed_documents(self) -> int:
        return 0

    def registration_param(self, tag: int, report: int = 1) -> bytes | None:
        """
        Правдоподобная эмуляция FF0Eh: единственный отчёт №1, тег 1209
        подчиняется state["ffd"] (тесты защёлки переключают им версию),
        остальные теги — как сняты с живой кассы 25.08.2026.

        state["ffd"] is None — «версия не определяется»: как и настоящая
        касса, отвечаем KKTError 0x37 «команда не поддерживается», причём
        для любого тега — на живой кассе FF0Eh целиком отказывает, а не
        выборочно по тегам.
        """
        if self.state["ffd"] is None:
            raise shtrih.KKTError(0x37, "Команда не поддерживается")
        if report != 1:
            return None
        values = {
            shtrih.TAG_FFD_VERSION: self.state["ffd"],
            shtrih.TAG_FFD_KKT: 2,       # ККТ умеет максимум 1.05
            shtrih.TAG_FFD_FN: 4,        # ФН умеет 1.2
        }
        if tag not in values:
            return None
        return bytes([values[tag]])

    def last_registration_report(self, limit: int = 20) -> int:
        return 1

    def ffd_versions(self) -> dict:
        """Согласовано с registration_param(): читает те же теги через него,
        поэтому state["ffd"] управляет обеими командами одинаково."""
        report = self.last_registration_report()

        def read(tag: int) -> int | None:
            value = self.registration_param(tag, report)
            return int.from_bytes(value, "little") if value else None

        return {
            "report": report,
            "current": read(shtrih.TAG_FFD_VERSION),
            "kkt": read(shtrih.TAG_FFD_KKT),
            "fn": read(shtrih.TAG_FFD_FN),
        }

    # -- время и дата --

    def set_time(self, t):
        if self.state["shift_open"]:
            raise shtrih.KKTError(0x3C, "Смена открыта операция невозможна")
        if self.state["date_pending"] is not None:
            raise shtrih.KKTError(0x73, "Неверный режим ККТ")
        real_now = datetime.now()
        current = real_now + timedelta(seconds=self.state["clock_offset"])
        target = current.replace(hour=t.hour, minute=t.minute, second=t.second,
                                  microsecond=0)
        self.state["clock_offset"] = (target - real_now).total_seconds()
        self._note(f"установлено время кассы {t.hour:02d}:{t.minute:02d}:{t.second:02d}")

    def set_date(self, d: date):
        if self.state["shift_open"]:
            raise shtrih.KKTError(0x3C, "Смена открыта операция невозможна")
        self.state["date_pending"] = d
        return self.confirm_date(d)

    def confirm_date(self, d: date):
        if self.state["date_pending"] is None or self.state["date_pending"] != d:
            raise shtrih.KKTError(0x7C, "Не совпадает дата")
        real_now = datetime.now()
        current = real_now + timedelta(seconds=self.state["clock_offset"])
        target = current.replace(year=d.year, month=d.month, day=d.day)
        self.state["clock_offset"] = (target - real_now).total_seconds()
        self.state["date_pending"] = None
        self._note(f"дата кассы подтверждена: {d.strftime('%d.%m.%Y')}")

    # -- смена --

    def open_shift(self):
        if self.state["shift_open"]:
            raise shtrih.KKTError(0x67, "Смена уже открыта")
        self.state["shift_open"] = True
        self.state["shift_number"] += 1
        self.state["receipt_number"] = 0
        self._note("открыта смена")

    def x_report(self):
        self._note("напечатан X-отчёт")

    def z_report(self):
        if not self.state["shift_open"]:
            raise shtrih.KKTError(0x67, "Смена не открыта")
        self.state["shift_open"] = False
        self.state["last_fd"] += 1
        self._note("напечатан Z-отчёт, смена закрыта")

    # -- чек --

    def open_receipt(self, doc_type: int = shtrih.DOC_SALE):
        if not self.state["shift_open"]:
            raise shtrih.KKTError(0x67, "Смена не открыта")
        self.state["receipt_open"] = True
        self._note(f"открыт чек типа {doc_type}")

    def cancel_receipt(self):
        self.state["receipt_open"] = False
        self._note("чек аннулирован")

    def operation(self, *, name: str, qty: float, price: float, **kw):
        if not self.state["receipt_open"]:
            raise shtrih.KKTError(0x67, "Чек не открыт")
        self._note(f"позиция: {name} {qty} x {price}")

    def close_receipt(self, *, cash: float = 0, electronic: float = 0, **kw) -> dict:
        if not self.state["receipt_open"]:
            raise shtrih.KKTError(0x67, "Чек не открыт")
        self.state["receipt_open"] = False
        self.state["receipt_number"] += 1
        self.state["last_fd"] += 1
        self._note("чек закрыт")
        return {"change": 0.0, "fd_number": self.state["last_fd"],
                "fiscal_sign": 1000000000 + self.state["last_fd"]}

    def send_tlv(self, structure: bytes):
        self._note(f"передан реквизит, {len(structure)} байт")

    def correction(self, *, total: float, reason_description: str = "",
                   reason_date: date | None = None, **kw) -> dict:
        if not self.state["shift_open"]:
            raise shtrih.KKTError(0x67, "Смена не открыта")
        self.state["receipt_number"] += 1
        self.state["last_fd"] += 1
        self._note(f"чек коррекции на {total}: {reason_description}")
        return {"receipt_number": self.state["receipt_number"],
                "fd_number": self.state["last_fd"],
                "fiscal_sign": 2000000000 + self.state["last_fd"]}
