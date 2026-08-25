"""
Работа с ККТ Штрих-М по TCP на уровне протокола.

Спецификация: «Протокол работы ККТ с ФН», v.1.18 (сборка 2.0.27 от 21.01.2025).

Формат кадра:
    STX(0x02) | LEN | CMD[...] | DATA[...] | LRC

    LEN — длина CMD+DATA (не включает STX, сам байт LEN и LRC)
    LRC — XOR всех байтов кроме STX, т.е. по LEN+CMD+DATA

Обмен (проверено на живой ККТ Штрих-М-02Ф, прошивка C1 сборка 62922):
    хост -> ККТ: ENQ(0x05)          — «свободна?»
    ККТ  -> хост: NAK(0x15)         — свободна, можно слать команду
                  ACK(0x06)         — есть неотправленный ответ, его надо вычитать
    хост -> ККТ: кадр команды
    ККТ  -> хост: ACK(0x06)
    ККТ  -> хост: кадр ответа
    хост -> хост: ACK(0x06)

ENQ обязателен ПЕРЕД КАЖДОЙ командой. Без него ККТ отвечает NAK на кадр
и команда теряется — на это напоролись в первой версии: работали только
вторая и последующие команды в соединении, первая всегда терялась.

Все числа — little-endian. Суммы — в копейках.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from datetime import date, datetime, time as _time

STX = 0x02
ENQ = 0x05
ACK = 0x06
NAK = 0x15

# --- Коды команд ---------------------------------------------------------

CMD_SHORT_STATUS = b"\x10"       # Короткий запрос состояния
CMD_LONG_STATUS = b"\x11"        # Запрос состояния ККТ
CMD_BEEP = b"\x13"               # Гудок
CMD_SET_TIME = b"\x21"           # Программирование времени
CMD_SET_DATE = b"\x22"           # Программирование даты
CMD_CONFIRM_DATE = b"\x23"       # Подтверждение программирования даты
CMD_CUT = b"\x25"                # Отрезка чека
CMD_X_REPORT = b"\x40"           # Суточный отчёт без гашения (X)
CMD_Z_REPORT = b"\x41"           # Суточный отчёт с гашением (Z)
CMD_ERROR_NAME = b"\x6b"         # Возврат названия ошибки
CMD_CANCEL_RECEIPT = b"\x88"     # Аннулирование чека
CMD_OPEN_RECEIPT = b"\x8d"       # Открыть чек
CMD_OPEN_SHIFT = b"\xe0"         # Открыть смену
CMD_DEVICE_TYPE = b"\xfc"        # Получить тип устройства

CMD_FN_STATUS = b"\xff\x01"      # Запрос статуса ФН
CMD_FN_NUMBER = b"\xff\x02"      # Запрос номера ФН
CMD_FN_EXPIRY = b"\xff\x03"      # Запрос срока действия ФН
CMD_FN_VERSION = b"\xff\x04"     # Запрос версии ФН
CMD_FISCALIZATION = b"\xff\x09"  # Запрос итогов последней фискализации
CMD_SEND_TLV = b"\xff\x0c"       # Передать произвольную TLV структуру
CMD_CORRECTION_BEGIN = b"\xff\x35"   # Начать формирование чека коррекции
CMD_OFD_STATUS = b"\xff\x39"     # Статус информационного обмена с ОФД
CMD_UNCONFIRMED = b"\xff\x3f"    # Запрос количества ФД без квитанции ОФД
CMD_SHIFT_PARAMS = b"\xff\x40"   # Запрос параметров текущей смены
CMD_CLOSE_RECEIPT_V2 = b"\xff\x45"   # Закрытие чека расширенное, вариант 2
CMD_OPERATION_V2 = b"\xff\x46"       # Операция V2 (позиция чека)
CMD_CORRECTION_V2 = b"\xff\x4a"      # Сформировать чек коррекции V2

# Команды, при которых ККТ печатает: ответ приходит после протяжки бумаги,
# ждать надо дольше обычного.
PRINTING_TIMEOUT = 90.0

# --- Справочники ---------------------------------------------------------

# Налоговая ставка (1 байт), команда FF46
VAT_RATES = {
    "20": 0x01,
    "10": 0x02,
    "0": 0x04,
    "none": 0x08,     # без НДС
    "20/120": 0x10,
    "10/110": 0x20,
    "5": 0x81,
    "7": 0x82,
    "5/105": 0x84,
    "7/107": 0x88,
}

# Система налогообложения — битовое поле (1 байт)
TAX_SYSTEMS = {
    "osn": 0x01,          # ОСН
    "usn_income": 0x02,   # УСН доход
    "usn_profit": 0x04,   # УСН доход минус расход
    "envd": 0x08,         # ЕНВД
    "esn": 0x10,          # ЕСП
    "psn": 0x20,          # ПСН
}

# Человеко-читаемые названия систем налогообложения. Коды берутся из
# TAX_SYSTEMS, здесь только подписи для интерфейса — не дублировать биты.
TAX_SYSTEM_NAMES = {
    "osn": "ОСН",
    "usn_income": "УСН доход",
    "usn_profit": "УСН доход минус расход",
    "envd": "ЕНВД",
    "esn": "ЕСП",
    "psn": "ПСН",
}

# Тип операции (FF46, FF4A)
OP_INCOME = 1          # Приход
OP_INCOME_RETURN = 2   # Возврат прихода
OP_EXPENSE = 3         # Расход
OP_EXPENSE_RETURN = 4  # Возврат расхода

# Тип документа для команды 8D «Открыть чек»
DOC_SALE = 0
DOC_BUY = 1
DOC_SALE_RETURN = 2
DOC_BUY_RETURN = 3

# Признак способа расчёта (тег 1214), ФФД 1.05
PAYMENT_METHODS = {
    1: "Предоплата 100%",
    2: "Частичная предоплата",
    3: "Аванс",
    4: "Полный расчёт",
    5: "Частичный расчёт и кредит",
    6: "Передача в кредит",
    7: "Оплата кредита",
}

# Признак предмета расчёта (тег 1212), ФФД 1.05
PAYMENT_SUBJECTS = {
    1: "Товар",
    2: "Подакцизный товар",
    3: "Работа",
    4: "Услуга",
    5: "Ставка азартной игры",
    10: "Платёж",
    11: "Агентское вознаграждение",
    13: "Иной предмет расчёта",
}

# Режимы ККТ (ответ команды 10h, младший полубайт)
ECR_MODES = {
    1: "Выдача данных",
    2: "Смена открыта, 24 часа не кончились",
    3: "Смена открыта, 24 часа кончились",
    4: "Смена закрыта",
    5: "Блокировка по паролю налогового инспектора",
    6: "Ожидание подтверждения даты",
    7: "Разрешение изменения положения десятичной точки",
    8: "Открытый документ",
    9: "Режим разрешения технологического обнуления",
    10: "Тестовый прогон",
    11: "Печать полного фискального отчёта",
    12: "Печать отчёта ЭКЛЗ",
    13: "Работа с фискальным подкладным документом",
    14: "Печать подкладного документа",
    15: "Фискальный подкладной документ сформирован",
}

# Типы фискальных документов (ответ FF01h, байт «текущий документ»)
FN_DOCUMENTS = {
    0x00: "нет открытого документа",
    0x01: "отчёт о фискализации",
    0x02: "отчёт об открытии смены",
    0x04: "кассовый чек",
    0x08: "отчёт о закрытии смены",
    0x10: "отчёт о закрытии фискального режима",
    0x14: "кассовый чек коррекции",
    0x17: "отчёт о текущем состоянии расчётов",
}


class KKTError(Exception):
    """Ошибка, вернувшаяся от самой ККТ (ненулевой код ошибки в ответе)."""

    def __init__(self, code: int, name: str = ""):
        self.code = code
        self.name = name
        super().__init__(f"Ошибка ККТ 0x{code:02X}" + (f": {name}" if name else ""))


class ProtocolError(Exception):
    """Ошибка обмена: нет ACK, битый LRC, обрыв связи."""


# --- Упаковка значений ---------------------------------------------------

def money(rub: float | int | str) -> bytes:
    """Сумма в рублях -> 5 байт копеек, little-endian."""
    kopecks = int(round(float(rub) * 100))
    if not 0 <= kopecks <= 0xFFFFFFFFFF:
        raise ValueError(f"Сумма вне диапазона: {rub}")
    return kopecks.to_bytes(5, "little")


def quantity(qty: float | int | str) -> bytes:
    """Количество -> 6 байт, 6 знаков после запятой, little-endian."""
    units = int(round(float(qty) * 1_000_000))
    if not 0 <= units <= 0xFFFFFFFFFFFF:
        raise ValueError(f"Количество вне диапазона: {qty}")
    return units.to_bytes(6, "little")


def password(pwd: int) -> bytes:
    """Пароль оператора или системного администратора -> 4 байта."""
    return int(pwd).to_bytes(4, "little")


def _date_field(d: date) -> bytes:
    """Дата -> «ДД ММ ГГ» обычными двоичными байтами, для команд 22h/23h."""
    if not 2000 <= d.year <= 2099:
        raise ValueError(f"Год вне диапазона 2000..2099: {d.year}")
    return bytes([d.day, d.month, d.year - 2000])


def _time_field(t) -> bytes:
    """Время -> «ЧЧ ММ СС» обычными двоичными байтами, для команды 21h."""
    return bytes([t.hour, t.minute, t.second])


def text_field(s: str, length: int) -> bytes:
    """Строка в CP1251, дополненная нулями до фиксированной длины."""
    raw = (s or "").encode("cp1251", errors="replace")[:length]
    return raw + b"\x00" * (length - len(raw))


def tlv(tag: int, value: bytes) -> bytes:
    """TLV-структура: тег (2 байта LE) + длина (2 байта LE) + значение."""
    return struct.pack("<HH", tag, len(value)) + value


def correction_reason_tlv(description: str, doc_date: date, doc_number: str) -> bytes:
    """
    Тег 1174 «Основание для коррекции» — обязателен в чеке коррекции ФФД 1.05.

    Вложенные теги:
        1177 — описание коррекции
        1178 — дата документа основания (unixtime, 4 байта)
        1179 — номер документа основания
    """
    midnight = datetime.combine(doc_date, _time())
    inner = (
        tlv(1177, description.encode("cp1251", errors="replace"))
        + tlv(1178, struct.pack("<I", int(midnight.timestamp())))
        + tlv(1179, doc_number.encode("cp1251", errors="replace"))
    )
    return tlv(1174, inner)


def corrected_receipt_tlv(fpd: str) -> bytes:
    """
    Тег 1192 «Дополнительный реквизит чека (БСО)» — ФПД исправляемого чека.

    По методическим рекомендациям ФНС по исправлению ошибок при расчётах
    кладётся и в обратный чек (возврат прихода), и в последующий исправленный
    чек, чтобы налоговая видела связь документов. Обязательность самая
    низкая («рекомендовано»), отсутствие тега нарушением не является.
    """
    return tlv(1192, fpd.encode("cp1251", errors="replace"))


def build_frame(command: bytes, data: bytes = b"") -> bytes:
    """Собрать кадр целиком: STX | LEN | CMD | DATA | LRC."""
    body = command + data
    if len(body) > 255:
        raise ValueError(f"Кадр длиннее 255 байт: {len(body)}")
    frame_body = bytes([len(body)]) + body
    return bytes([STX]) + frame_body + bytes([lrc(frame_body)])


def lrc(payload: bytes) -> int:
    """XOR всех байтов, начиная с байта длины."""
    result = 0
    for b in payload:
        result ^= b
    return result


# --- Разбор ответов ------------------------------------------------------

def _bcd_date(b: bytes) -> str:
    """3 байта ДД ММ ГГ -> «ДД.ММ.20ГГ»."""
    return f"{b[0]:02d}.{b[1]:02d}.{2000 + b[2]}"


def _clock(b: bytes) -> str:
    """3 байта ЧЧ ММ СС -> «ЧЧ:ММ:СС»."""
    return f"{b[0]:02d}:{b[1]:02d}:{b[2]:02d}"


@dataclass
class Response:
    command: bytes
    error_code: int
    data: bytes = b""

    @property
    def ok(self) -> bool:
        return self.error_code == 0


class KKT:
    """
    Соединение с ККТ по TCP.

    Использование:
        with KKT("192.168.88.107", 7778) as kkt:
            print(kkt.short_status())
    """

    def __init__(self, host: str, port: int = 7778, timeout: float = 15.0,
                 connect_timeout: float = 3.0,
                 operator_password: int = 30, admin_password: int = 30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.operator_password = operator_password
        self.admin_password = admin_password
        self._sock: socket.socket | None = None
        self.log: list[str] = []

    # -- журнал --

    def _note(self, line: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {line}")
        if len(self.log) > 200:                      # журнал не растёт бесконечно
            del self.log[:-200]

    # -- соединение --

    def connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection(
            (self.host, self.port), self.connect_timeout
        )
        self._sock.settimeout(self.timeout)
        self._note(f"соединение с {self.host}:{self.port}")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "KKT":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- низкий уровень --

    def _read_exactly(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ProtocolError("ККТ закрыла соединение")
            buf += chunk
        return buf

    def _read_frame(self) -> bytes:
        """Прочитать кадр ответа, проверить LRC, подтвердить ACK. Вернуть payload."""
        while self._read_exactly(1)[0] != STX:
            pass                                     # мусор до STX игнорируем
        length = self._read_exactly(1)[0]
        payload = self._read_exactly(length)
        got = self._read_exactly(1)[0]
        want = lrc(bytes([length]) + payload)
        if got != want:
            self._sock.sendall(bytes([NAK]))
            raise ProtocolError(
                f"Не сошлась контрольная сумма ответа: получено 0x{got:02X}, "
                f"ожидалось 0x{want:02X}"
            )
        self._sock.sendall(bytes([ACK]))
        self._note(f"<- {(bytes([STX, length]) + payload + bytes([got])).hex(' ').upper()}")
        return payload

    def _handshake(self) -> None:
        """
        ENQ перед командой. NAK — ККТ свободна. ACK — у неё висит недоотданный
        ответ, его надо вычитать, иначе следующий обмен разъедется.
        """
        for attempt in range(3):
            self._sock.sendall(bytes([ENQ]))
            reply = self._read_exactly(1)[0]
            if reply == NAK:
                return
            if reply == ACK:
                self._note("ENQ -> ACK: вычитываю зависший ответ")
                self._read_frame()
                continue
            self._note(f"ENQ -> 0x{reply:02X}, повтор")
        raise ProtocolError("ККТ не подтверждает готовность принять команду")

    def send(self, command: bytes, data: bytes = b"",
             timeout: float | None = None) -> Response:
        """Отправить команду и получить разобранный ответ. Не проверяет код ошибки."""
        if self._sock is None:
            self.connect()

        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            self._handshake()

            frame = build_frame(command, data)
            for attempt in range(3):
                self._note(f"-> {frame.hex(' ').upper()}")
                self._sock.sendall(frame)
                reply = self._read_exactly(1)[0]
                if reply == ACK:
                    break
                if reply == NAK:
                    self._note("ККТ ответила NAK на кадр, повтор")
                    self._handshake()
                    continue
                raise ProtocolError(f"Ожидался ACK, получено 0x{reply:02X}")
            else:
                raise ProtocolError("ККТ трижды отвергла кадр команды (NAK)")

            payload = self._read_frame()
        finally:
            if timeout is not None and self._sock is not None:
                self._sock.settimeout(self.timeout)

        cmd_len = len(command)
        resp_cmd = payload[:cmd_len]
        return Response(
            command=resp_cmd,
            error_code=payload[cmd_len],
            data=payload[cmd_len + 1:],
        )

    def execute(self, command: bytes, data: bytes = b"",
                timeout: float | None = None) -> Response:
        """Отправить команду и поднять исключение, если ККТ вернула ошибку."""
        resp = self.send(command, data, timeout=timeout)
        if not resp.ok:
            raise KKTError(resp.error_code, self.error_name(resp.error_code))
        return resp

    def error_name(self, code: int) -> str:
        """
        Спросить у самой ККТ текст ошибки (команда 6Bh).

        На прошивке C1/62922 команда не поддерживается — возвращает собственную
        ошибку 0x37. Поэтому это лучшее усилие, а не гарантия: если текста нет,
        вызывающий показывает голый код.
        """
        if code == 0:
            return ""
        try:
            resp = self.send(CMD_ERROR_NAME, bytes([code]))
            if resp.ok and resp.data:
                return resp.data.split(b"\x00")[0].decode("cp1251", errors="replace").strip()
        except Exception:
            pass
        return ""

    # -- состояние --

    def short_status(self) -> dict:
        """
        Команда 10h. Режим, подрежим, флаги.

        Раскладка ответа (проверена на живой ККТ):
            0     номер оператора
            1-2   флаги ККТ
            3     режим ККТ
            4     подрежим
            5     количество операций в чеке, младший байт
            6     напряжение резервной батареи
            7     напряжение источника питания
            8-13  служебные поля, значения плавают от запроса к запросу
        """
        r = self.execute(CMD_SHORT_STATUS, password(self.operator_password))
        d = r.data
        flags = int.from_bytes(d[1:3], "little")
        mode_byte = d[3]
        mode = mode_byte & 0x0F
        return {
            "operator": d[0],
            "flags": flags,
            "flags_hex": f"0x{flags:04X}",
            "mode": mode,
            "mode_name": ECR_MODES.get(mode, f"Режим {mode}"),
            "mode_status": (mode_byte >> 4) & 0x0F,
            "submode": d[4],
            "operations_in_receipt": d[5],
            "paper": bool(flags & (1 << 1)),     # бит 1 — рулон чековой ленты
            "receipt_open": mode == 8,
        }

    def long_status(self) -> dict:
        """
        Команда 11h. Заводской номер, ИНН, дата и время, версия ПО.

        Раскладка ответа (проверена на живой ККТ: дата и время сошлись
        с настенными часами, заводской номер — с наклейкой):
            0      номер оператора
            1-2    версия ПО ККТ
            3-4    сборка ПО ККТ
            5-7    дата ПО ККТ (ДД ММ ГГ)
            8      номер в зале
            9-10   сквозной номер текущего документа
            11-12  флаги ККТ
            13     режим ККТ
            14     подрежим
            15     порт ККТ
            16-17  версия ПО ФП
            18-19  сборка ПО ФП
            20-22  дата ПО ФП
            23-25  дата (ДД ММ ГГ)
            26-28  время (ЧЧ ММ СС)
            29     флаги ФП
            30-33  заводской номер
            34-35  номер последней закрытой смены
            36-39  перерегистрации и служебные поля
            40-45  ИНН (смещение подтверждено: значение проходит контрольную
                   сумму ИНН, при сдвиге на 38 или 42 — не проходит)
        """
        r = self.execute(CMD_LONG_STATUS, password(self.operator_password))
        d = r.data
        inn = int.from_bytes(d[40:46], "little")
        mode = d[13] & 0x0F
        return {
            "operator": d[0],
            "sw_version": d[1:3].decode("cp1251", errors="replace"),
            "sw_build": int.from_bytes(d[3:5], "little"),
            "sw_date": _bcd_date(d[5:8]),
            "doc_number": int.from_bytes(d[9:11], "little"),
            "flags": int.from_bytes(d[11:13], "little"),
            "mode": mode,
            "mode_name": ECR_MODES.get(mode, f"Режим {mode}"),
            "submode": d[14],
            "port": d[15],
            "date": _bcd_date(d[23:26]),
            "time": _clock(d[26:29]),
            "serial": int.from_bytes(d[30:34], "little"),
            "last_closed_shift": int.from_bytes(d[34:36], "little"),
            "inn": inn if inn not in (0, 0xFFFFFFFFFFFF) else None,
        }

    def shift_params(self) -> dict:
        """Команда FF40h. Состояние смены, номер смены, номер чека."""
        r = self.execute(CMD_SHIFT_PARAMS, password(self.admin_password))
        d = r.data
        return {
            "shift_open": d[0] == 1,
            "shift_number": int.from_bytes(d[1:3], "little"),
            "receipt_number": int.from_bytes(d[3:5], "little"),
        }

    def fn_status(self) -> dict:
        """
        Команда FF01h. Фаза жизни ФН, открытый документ, номер ФН.

        Раскладка ответа (проверена на живой ККТ: номер ФН — 16 цифр,
        сошёлся с наклейкой на корпусе):
            0      состояние фазы жизни
            1      текущий документ
            2      данные документа
            3      состояние смены
            4      флаги предупреждений
            5-9    дата и время последнего документа (ГГ ММ ДД ЧЧ ММ)
            10-25  номер ФН, 16 символов ASCII
            26-29  номер последнего ФД
        """
        r = self.execute(CMD_FN_STATUS, password(self.admin_password))
        d = r.data
        phase = d[0]
        y, mo, dd, hh, mi = d[5:10]
        return {
            "configured": bool(phase & 0x01),
            "fiscal_mode_open": bool(phase & 0x02),
            "fiscal_mode_closed": bool(phase & 0x04),
            "current_document": FN_DOCUMENTS.get(d[1], f"код 0x{d[1]:02X}"),
            "has_document_data": d[2] == 1,
            "shift_open": d[3] == 1,
            "warnings": d[4],
            "last_document_at": f"{dd:02d}.{mo:02d}.{2000 + y} {hh:02d}:{mi:02d}",
            "fn_number": d[10:26].decode("ascii", errors="replace").strip("\x00 "),
            "last_fd": int.from_bytes(d[26:30], "little"),
        }

    def ofd_status(self) -> dict:
        """Команда FF39h. Очередь непереданных документов в ОФД."""
        r = self.execute(CMD_OFD_STATUS, password(self.admin_password))
        d = r.data
        return {
            "connected": bool(d[0] & 0x01),
            "has_message": bool(d[0] & 0x02),
            "waiting_receipt": bool(d[0] & 0x04),
            "queue_length": int.from_bytes(d[2:4], "little"),
            "first_document": int.from_bytes(d[4:8], "little"),
        }

    def fn_expiry(self) -> dict:
        """
        Команда FF03h. Срок действия ФН.

        Спецификация v.1.18 обещает 3 байта данных (ГГ ММ ДД), прошивка
        C1/62922 на живой кассе вернула 5 — два лишних байта после даты
        спецификацией не описаны, их назначение не установлено, разбор
        отдаёт их сырьём и не зависит от того, 3 байта пришло или 5.
        """
        r = self.execute(CMD_FN_EXPIRY, password(self.admin_password))
        d = r.data
        expiry = f"{d[2]:02d}.{d[1]:02d}.{2000 + d[0]}"
        return {
            "expiry": expiry,
            "tail": d[3:].hex(" ").upper(),
        }

    def fn_version(self) -> dict:
        """
        Команда FF04h. Версия ПО ФН.

        Раскладка ответа (проверена на живой ККТ):
            0-15   версия ПО ФН, ASCII, дополнена пробелами и нулём
            16     тип ПО: 0 — отладочная, 1 — серийная
        """
        r = self.execute(CMD_FN_VERSION, password(self.admin_password))
        d = r.data
        return {
            "version": d[0:16].decode("ascii", errors="replace").strip(" \x00"),
            "serial_software": d[16] == 1,
        }

    def fiscalization(self) -> dict:
        """
        Команда FF09h. Итоги последней фискализации (перерегистрации).

        Спецификация обещает 47 байт данных для ФФД 1.05, прошивка C1/62922
        на живой кассе вернула 48. Раскладка сверена по четырём независимым
        признакам (ИНН совпал с ответом 11h, РН ККТ — 16 цифр, код СНО
        совпал с настройкой tax_system, номер ФД = 1, как и положено отчёту
        о регистрации):
            0-4    дата и время фискализации: ГГ ММ ДД ЧЧ ММ
            5-16   ИНН, 12 байт ASCII
            17-36  регистрационный номер ККТ, 20 байт ASCII
            37     код системы налогообложения, битовое поле
            38     режим работы, битовое поле
            39     расширенные признаки работы ККТ (лишний байт
                   относительно спецификации, не разбирается)
            40-43  номер ФД, uint32 LE
            44-47  фискальный признак, uint32 LE
        """
        r = self.execute(CMD_FISCALIZATION, password(self.admin_password))
        d = r.data
        y, mo, dd, hh, mi = d[0:5]
        tax_bits = d[37]
        tax_systems = [
            name for key, name in TAX_SYSTEM_NAMES.items() if tax_bits & TAX_SYSTEMS[key]
        ]
        return {
            "at": f"{dd:02d}.{mo:02d}.{2000 + y} {hh:02d}:{mi:02d}",
            "inn": d[5:17].decode("ascii", errors="replace").strip(),
            "reg_number": d[17:37].decode("ascii", errors="replace").strip(),
            "tax_systems": tax_systems,
            "work_modes": d[38],
            "fd": int.from_bytes(d[40:44], "little"),
            "fp": int.from_bytes(d[44:48], "little"),
        }

    def unconfirmed_documents(self) -> int:
        """Команда FF3Fh. Количество ФД, на которые нет квитанции ОФД."""
        r = self.execute(CMD_UNCONFIRMED, password(self.admin_password))
        return int.from_bytes(r.data[0:2], "little")

    def device_type(self) -> dict:
        """
        Команда FCh. Модель и название устройства.

        Ответ живой ККТ: 00 00 00 01 0E FA 00 «ШТРИХ-М-02Ф» —
        код ошибки, тип, подтип, версия 1.14 протокола, модель 0xFA, язык.
        """
        r = self.execute(CMD_DEVICE_TYPE)
        d = r.data
        return {
            "type": d[0],
            "subtype": d[1],
            "protocol": f"{d[2]}.{d[3]}",
            "model": d[4],
            "language": d[5],
            "name": d[6:].decode("cp1251", errors="replace").strip("\x00 "),
        }

    # -- время и дата --

    def set_time(self, t) -> Response:
        """Команда 21h. Программирование времени ККТ («ЧЧ ММ СС»)."""
        return self.execute(CMD_SET_TIME, password(self.admin_password) + _time_field(t))

    def set_date(self, d: date) -> Response:
        """
        Команда 22h, затем 23h. Программирование даты ККТ.

        После 22h касса встаёт в режим 6 «Ожидание подтверждения даты» и не
        выходит из него, пока не придёт 23h с той же датой, — поэтому оба
        кадра шлём одним вызовом, чтобы подтверждение нельзя было забыть.
        """
        self.execute(CMD_SET_DATE, password(self.admin_password) + _date_field(d))
        return self.confirm_date(d)

    def confirm_date(self, d: date) -> Response:
        """Команда 23h. Подтверждение программирования даты — выход из режима 6."""
        return self.execute(CMD_CONFIRM_DATE, password(self.admin_password) + _date_field(d))

    # -- смена --

    def open_shift(self) -> Response:
        """Команда E0h. Открыть смену."""
        return self.execute(
            CMD_OPEN_SHIFT, password(self.operator_password), timeout=PRINTING_TIMEOUT
        )

    def x_report(self) -> Response:
        """Команда 40h. Суточный отчёт без гашения."""
        return self.execute(
            CMD_X_REPORT, password(self.admin_password), timeout=PRINTING_TIMEOUT
        )

    def z_report(self) -> Response:
        """Команда 41h. Суточный отчёт с гашением — закрывает смену."""
        return self.execute(
            CMD_Z_REPORT, password(self.admin_password), timeout=PRINTING_TIMEOUT
        )

    # -- чек --

    def open_receipt(self, doc_type: int = DOC_SALE) -> Response:
        """Команда 8Dh. Открыть чек заданного типа."""
        return self.execute(
            CMD_OPEN_RECEIPT, password(self.operator_password) + bytes([doc_type])
        )

    def cancel_receipt(self) -> Response:
        """Команда 88h. Аннулировать открытый чек."""
        return self.execute(
            CMD_CANCEL_RECEIPT, password(self.operator_password),
            timeout=PRINTING_TIMEOUT,
        )

    def operation(self, *, op_type: int, qty: float, price: float, name: str,
                  vat: str = "none", department: int = 0,
                  payment_method: int = 4, payment_subject: int = 4,
                  total: float | None = None, vat_sum: float | None = None) -> Response:
        """
        Команда FF46h «Операция V2» — позиция чека.

        total=None  -> сумма считается кассой как цена x количество (0xFF..FF)
        vat_sum=None -> сумма налога не указывается, касса считает сама
        """
        if vat not in VAT_RATES:
            raise ValueError(f"Неизвестная ставка НДС: {vat}")
        no_value = b"\xff\xff\xff\xff\xff"
        data = (
            password(self.operator_password)
            + bytes([op_type])
            + quantity(qty)
            + money(price)
            + (no_value if total is None else money(total))
            + (no_value if vat_sum is None else money(vat_sum))
            + bytes([VAT_RATES[vat]])
            + bytes([department])
            + bytes([payment_method])
            + bytes([payment_subject])
            + text_field(name, 128)
        )
        return self.execute(CMD_OPERATION_V2, data, timeout=PRINTING_TIMEOUT)

    def close_receipt(self, *, cash: float = 0, electronic: float = 0,
                      prepay: float = 0, postpay: float = 0, barter: float = 0,
                      tax_system: str = "usn_income", text: str = "",
                      rounding: int = 0) -> dict:
        """
        Команда FF45h «Закрытие чека расширенное, вариант 2».

        Типы оплаты 2-13 при передаче в ОФД суммируются как «ЭЛЕКТРОННЫМИ».
        Налоги при режиме начисления 0 (таблица 1) касса считает сама.
        """
        if tax_system not in TAX_SYSTEMS:
            raise ValueError(f"Неизвестная система налогообложения: {tax_system}")
        zero = money(0)
        payments = (
            money(cash)
            + money(electronic)          # тип 2
            + zero * 11                  # типы 3-13
            + money(prepay)              # тип 14
            + money(postpay)             # тип 15
            + money(barter)              # тип 16
        )
        data = (
            password(self.admin_password)
            + payments
            + bytes([rounding])
            + zero * 6                   # налоги 1-6, считает касса
            + bytes([TAX_SYSTEMS[tax_system]])
            + text_field(text, 64)
        )
        r = self.execute(CMD_CLOSE_RECEIPT_V2, data, timeout=PRINTING_TIMEOUT)
        d = r.data
        return {
            "change": int.from_bytes(d[0:5], "little") / 100,
            "fd_number": int.from_bytes(d[5:9], "little"),
            "fiscal_sign": int.from_bytes(d[9:13], "little"),
        }

    def send_tlv(self, structure: bytes) -> Response:
        """Команда FF0Ch «Передать произвольную TLV структуру»."""
        return self.execute(CMD_SEND_TLV, password(self.admin_password) + structure)

    # -- чек коррекции --

    def correction(self, *, correction_type: int, op_type: int, total: float,
                   cash: float = 0, electronic: float = 0,
                   tax_system: str = "usn_income",
                   reason_description: str = "", reason_date: date | None = None,
                   reason_number: str = "") -> dict:
        """
        Чек коррекции: FF35h (начать) -> FF0Ch (основание) -> FF4Ah (сформировать).

        correction_type: 0 — самостоятельно, 1 — по предписанию
        op_type: 1 приход, 2 возврат прихода, 3 расход, 4 возврат расхода
        """
        if tax_system not in TAX_SYSTEMS:
            raise ValueError(f"Неизвестная система налогообложения: {tax_system}")

        self.execute(CMD_CORRECTION_BEGIN, password(self.admin_password))

        # Тег 1174 «основание для коррекции» — обязателен для ФФД 1.05
        if reason_description or reason_number:
            reason = correction_reason_tlv(
                reason_description, reason_date or date.today(), reason_number
            )
            self.execute(CMD_SEND_TLV, password(self.admin_password) + reason)

        zero = money(0)
        data = (
            password(self.admin_password)
            + bytes([correction_type])
            + bytes([op_type])
            + money(total)
            + money(cash)
            + money(electronic)
            + zero * 3                   # предоплата, постоплата, встречное
            + zero * 6                   # суммы налогов, считает касса
            + bytes([TAX_SYSTEMS[tax_system]])
        )
        r = self.execute(CMD_CORRECTION_V2, data, timeout=PRINTING_TIMEOUT)
        d = r.data
        return {
            "receipt_number": int.from_bytes(d[0:2], "little"),
            "fd_number": int.from_bytes(d[2:6], "little"),
            "fiscal_sign": int.from_bytes(d[6:10], "little"),
        }
