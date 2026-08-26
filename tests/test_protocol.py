"""
Проверки протокольного слоя.

Эталонные кадры сняты с живой ККТ Штрих-М-02Ф (прошивка C1, сборка 62922) —
на них проверялись смещения полей. Если разбор поедет, тесты покажут это
раньше, чем касса напечатает кривой чек.

Идентификаторы кассы в кадрах заменены на синтетические: номер ФН,
заводской номер и ИНН — выдуманные. Раскладка байтов при этом сохранена
один в один, а подставной ИНН собран так, что проходит контрольную сумму:
именно она в своё время и подтвердила смещение поля.
"""

from datetime import date, time

import pytest

import shtrih


# --- Заглушка сокета -----------------------------------------------------

class FakeSocket:
    """
    Сокет, отвечающий по сценарию Штрих-М: NAK на ENQ, ACK на кадр, кадр ответа.

    Всё отправленное копится в `sent`, чтобы тест мог сверить кадры команд.
    """

    def __init__(self, payloads: list[bytes]):
        self.sent = bytearray()
        self.control: list[int] = []          # одиночные байты: ENQ, ACK, NAK
        self._out = bytearray()
        self._payloads = list(payloads)
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        pass

    def sendall(self, data: bytes):
        # Управляющие байты приходят по одному, кадр — целиком одним вызовом.
        # Сканировать содержимое кадра нельзя: 0x02 внутри суммы — обычный байт.
        self.sent += data
        if len(data) == 1:
            self.control.append(data[0])
            if data[0] == shtrih.ENQ:
                self._out.append(shtrih.NAK)          # касса свободна
            return
        self._out.append(shtrih.ACK)                  # кадр принят
        payload = self._payloads.pop(0)
        frame_body = bytes([len(payload)]) + payload
        self._out += (bytes([shtrih.STX]) + frame_body
                      + bytes([shtrih.lrc(frame_body)]))

    def recv(self, n: int) -> bytes:
        if not self._out:
            raise TimeoutError("заглушке нечего отдать")
        chunk = bytes(self._out[:n])
        del self._out[:n]
        return chunk


def kkt_with(payloads: list[bytes]) -> shtrih.KKT:
    k = shtrih.KKT("stub", 0)
    k._sock = FakeSocket(payloads)
    return k


def frames_sent(k: shtrih.KKT) -> list[bytes]:
    """Кадры команд, вычленённые из потока (ENQ и ACK отбрасываем)."""
    out, buf = [], bytes(k._sock.sent)
    i = 0
    while i < len(buf):
        if buf[i] == shtrih.STX:
            length = buf[i + 1]
            out.append(buf[i:i + length + 3])
            i += length + 3
        else:
            i += 1
    return out


# --- Упаковка значений ---------------------------------------------------

def test_money_в_копейках_little_endian():
    assert shtrih.money(0) == b"\x00\x00\x00\x00\x00"
    assert shtrih.money(1) == b"\x64\x00\x00\x00\x00"          # 100 копеек
    assert shtrih.money(150.50) == b"\xca\x3a\x00\x00\x00"     # 15050 копеек


def test_money_округляет_копейки_а_не_режет():
    # 0.1 + 0.2 в двоичной плавающей = 0.30000000000000004
    assert shtrih.money(0.1 + 0.2) == shtrih.money(0.30)


def test_money_отвергает_отрицательную_сумму():
    with pytest.raises(ValueError):
        shtrih.money(-1)


def test_quantity_шесть_знаков_после_запятой():
    assert shtrih.quantity(1) == (1_000_000).to_bytes(6, "little")
    assert shtrih.quantity(0.5) == (500_000).to_bytes(6, "little")


def test_text_field_кодирует_cp1251_и_добивает_нулями():
    got = shtrih.text_field("Чек", 8)
    assert len(got) == 8
    assert got.decode("cp1251").rstrip("\x00") == "Чек"


def test_text_field_обрезает_длинное_наименование():
    assert len(shtrih.text_field("я" * 500, 128)) == 128


# --- Кадр ----------------------------------------------------------------

def test_кадр_собирается_с_верной_контрольной_суммой():
    # Эталон снят с живой кассы: короткий запрос состояния с паролем 30
    frame = shtrih.build_frame(b"\x10", shtrih.password(30))
    assert frame == bytes([0x02, 0x05, 0x10, 0x1E, 0x00, 0x00, 0x00, 0x0B])


def test_lrc_это_xor_начиная_с_длины():
    assert shtrih.lrc(bytes([0x05, 0x10, 0x1E, 0x00, 0x00, 0x00])) == 0x0B


def test_кадр_длиннее_255_байт_не_собирается():
    with pytest.raises(ValueError):
        shtrih.build_frame(b"\x10", b"\x00" * 300)


# --- Рукопожатие ---------------------------------------------------------

def test_перед_каждой_командой_уходит_enq():
    """
    Без ENQ живая касса отвечает NAK на первый кадр в соединении, и команда
    теряется. Это ровно тот баг, из-за которого приборная панель молчала.
    """
    k = kkt_with([b"\x10\x00" + bytes(14), b"\x10\x00" + bytes(14)])
    k.short_status()
    k.short_status()
    # Считаем именно одиночные управляющие байты: 0x05 встречается и как ENQ,
    # и как байт длины внутри кадра, поиск по всему потоку тут врёт.
    assert k._sock.control.count(shtrih.ENQ) == 2
    assert bytes(k._sock.sent).startswith(bytes([shtrih.ENQ, shtrih.STX]))


def test_битая_контрольная_сумма_ответа_поднимает_ошибку():
    k = shtrih.KKT("stub", 0)

    class Broken(FakeSocket):
        def sendall(self, data):
            super().sendall(data)
            if data[:1] == bytes([shtrih.STX]):
                self._out[-1] ^= 0xFF        # портим LRC ответа

    k._sock = Broken([b"\x10\x00" + bytes(14)])
    with pytest.raises(shtrih.ProtocolError, match="контрольная сумма"):
        k.short_status()


# --- Длины команд (совпадают со спецификацией) ---------------------------

def test_операция_ff46_имеет_длину_160_байт():
    k = kkt_with([b"\xff\x46\x00"])
    k.operation(op_type=shtrih.OP_INCOME, qty=1, price=1, name="Стоянка")
    frame = frames_sent(k)[0]
    assert frame[1] == 160              # CMD(2) + DATA(158), как в спецификации
    assert len(frame) == 163            # плюс STX, байт длины и LRC


def test_закрытие_чека_ff45_имеет_длину_182_байта():
    k = kkt_with([b"\xff\x45\x00" + bytes(13)])
    k.close_receipt(cash=1)
    frame = frames_sent(k)[0]
    assert frame[1] == 182              # CMD(2) + DATA(180)
    assert len(frame) == 185


def test_коррекция_ff4a_имеет_длину_69_байт():
    k = kkt_with([b"\xff\x35\x00", b"\xff\x4a\x00" + bytes(10)])
    k.correction(correction_type=0, op_type=shtrih.OP_INCOME, total=1, cash=1)
    frame = frames_sent(k)[-1]
    assert frame[1] == 69               # CMD(2) + DATA(67)
    assert len(frame) == 72


def test_позиция_чека_уходит_под_паролем_оператора():
    k = shtrih.KKT("stub", 0, operator_password=77, admin_password=88)
    k._sock = FakeSocket([b"\xff\x46\x00"])
    k.operation(op_type=shtrih.OP_INCOME, qty=1, price=1, name="Услуга")
    assert frames_sent(k)[0][4:8] == shtrih.password(77)


def test_закрытие_чека_уходит_под_паролем_сисадмина():
    k = shtrih.KKT("stub", 0, operator_password=77, admin_password=88)
    k._sock = FakeSocket([b"\xff\x45\x00" + bytes(13)])
    k.close_receipt(cash=1)
    assert frames_sent(k)[0][4:8] == shtrih.password(88)


def test_неизвестная_ставка_ндс_не_доходит_до_кассы():
    k = kkt_with([])
    with pytest.raises(ValueError, match="ставка НДС"):
        k.operation(op_type=1, qty=1, price=1, name="X", vat="18")


# --- Основание коррекции (тег 1174) --------------------------------------

def test_основание_коррекции_содержит_три_вложенных_тега():
    raw = shtrih.correction_reason_tlv("не пробит чек", date(2026, 8, 20), "б/н")
    tag, length = int.from_bytes(raw[0:2], "little"), int.from_bytes(raw[2:4], "little")
    assert tag == 1174
    assert length == len(raw) - 4
    inner = raw[4:]
    tags = []
    i = 0
    while i < len(inner):
        t = int.from_bytes(inner[i:i + 2], "little")
        n = int.from_bytes(inner[i + 2:i + 4], "little")
        tags.append(t)
        i += 4 + n
    assert tags == [1177, 1178, 1179]


# --- Реквизит исправленного чека (тег 1192) -------------------------------

def test_реквизит_исправленного_чека_кодируется_тегом_1192():
    # Тег 1192 = 0x04A8 LE, длина значения 10 (цифр ФПД) LE, затем сами цифры
    raw = shtrih.corrected_receipt_tlv("2074148893")
    assert raw == bytes.fromhex("a8040a00") + b"2074148893"


def test_send_tlv_шлёт_ff0c_с_паролем_сисадмина_и_структурой():
    k = shtrih.KKT("stub", 0, operator_password=77, admin_password=88)
    k._sock = FakeSocket([b"\xff\x0c\x00"])
    structure = shtrih.corrected_receipt_tlv("2074148893")
    k.send_tlv(structure)
    frame = frames_sent(k)[0]
    assert frame[2:4] == shtrih.CMD_SEND_TLV
    assert frame[4:-1] == shtrih.password(88) + structure


# --- Время и дата (21h/22h/23h) -------------------------------------------

def test_set_time_шлёт_кадр_21h_с_временем_обычными_байтами():
    k = shtrih.KKT("stub", 0, admin_password=30)
    k._sock = FakeSocket([b"\x21\x00"])
    k.set_time(time(14, 5, 9))
    frames = frames_sent(k)
    assert len(frames) == 1
    frame = frames[0]
    assert frame[2:3] == shtrih.CMD_SET_TIME
    assert frame[1] == 8                      # CMD(1) + пароль(4) + ЧЧ ММ СС(3)
    assert frame[3:7] == shtrih.password(30)
    assert frame[7:10] == bytes([14, 5, 9])


def test_set_date_шлёт_ровно_два_кадра_22h_и_23h_с_одинаковым_хвостом():
    k = shtrih.KKT("stub", 0, admin_password=30)
    k._sock = FakeSocket([b"\x22\x00", b"\x23\x00"])
    k.set_date(date(2026, 8, 25))
    frames = frames_sent(k)
    assert len(frames) == 2
    assert frames[0][2:3] == shtrih.CMD_SET_DATE
    assert frames[1][2:3] == shtrih.CMD_CONFIRM_DATE
    tail = shtrih.password(30) + bytes([25, 8, 26])
    assert frames[0][3:10] == tail
    assert frames[1][3:10] == tail


def test_confirm_date_шлёт_один_кадр_23h_с_паролем_сисадмина_и_датой():
    k = shtrih.KKT("stub", 0, admin_password=30)
    k._sock = FakeSocket([b"\x23\x00"])
    k.confirm_date(date(2026, 1, 2))
    frames = frames_sent(k)
    assert len(frames) == 1
    assert frames[0][2:3] == shtrih.CMD_CONFIRM_DATE
    assert frames[0][3:10] == shtrih.password(30) + bytes([2, 1, 26])


def test_ошибка_0x7c_на_подтверждении_даты_поднимает_исключение():
    # Ответ с кодом ошибки 0x7C плюс ответ на попытку узнать её название
    k = kkt_with([b"\x23\x7c", b"\x6b\x37"])
    with pytest.raises(shtrih.KKTError) as e:
        k.confirm_date(date(2026, 8, 25))
    assert e.value.code == 0x7C


def test_date_field_отвергает_год_вне_2000_2099():
    with pytest.raises(ValueError):
        shtrih._date_field(date(1999, 12, 31))
    with pytest.raises(ValueError):
        shtrih._date_field(date(2100, 1, 1))


# --- Разбор эталонных ответов живой кассы --------------------------------

def test_разбор_короткого_статуса_живой_кассы():
    # Кадр снят с ККТ: смена закрыта, бумага есть
    payload = bytes.fromhex("10001e9202040000" "9d2ec8010074020000")[:16]
    k = kkt_with([payload])
    st = k.short_status()
    assert st["operator"] == 30
    assert st["mode"] == 4
    assert st["mode_name"] == "Смена закрыта"
    assert st["paper"] is True
    assert st["receipt_open"] is False
    assert st["operations_in_receipt"] == 0


def test_разбор_полного_статуса_живой_кассы():
    # Дата и время в кадре сверялись с настенными часами, номер — с шильдиком
    payload = bytes.fromhex(
        "1100"
        "1e43 31caf5 0a0119 01 1900 9202 04 00 02"
        "4e41 0000 010110"
        "14081a 141726 00"
        "39300000 0600 00 00"
        "00 00b3962991b300".replace(" ", "")
    )
    k = kkt_with([payload])
    st = k.long_status()
    assert st["sw_version"] == "C1"
    assert st["mode_name"] == "Смена закрыта"
    assert st["date"] == "20.08.2026"
    assert st["time"] == "20:23:38"
    assert st["serial"] == 12345
    assert st["last_closed_shift"] == 6
    assert st["fp_counters"] == "00 00 00 00"  # раскладка не подтверждена, отдаём сырьём
    assert st["inn"] == 771234567859         # 12 цифр, контрольная сумма сходится


def test_разбор_статуса_фн_живой_кассы():
    payload = bytes.fromhex(
        "ff0100"
        "03 00 00 00 00"
        "1a08121213"
        "39393939303738393030303031323334"
        "18000000".replace(" ", "")
    )
    k = kkt_with([payload])
    st = k.fn_status()
    assert st["fn_number"] == "9999078900001234"     # ровно 16 цифр, без мусора
    assert len(st["fn_number"]) == 16
    assert st["last_fd"] == 24
    assert st["current_document"] == "нет открытого документа"
    assert st["shift_open"] is False


def test_разбор_типа_устройства_живой_кассы():
    payload = bytes.fromhex("fc00" "000001 0efa00") + "ШТРИХ-М-02Ф".encode("cp1251")
    k = kkt_with([payload])
    d = k.device_type()
    assert d["name"] == "ШТРИХ-М-02Ф"
    assert d["protocol"] == "1.14"


def test_разбор_параметров_смены_живой_кассы():
    k = kkt_with([bytes.fromhex("ff400000060000 00".replace(" ", ""))])
    st = k.shift_params()
    assert st == {"shift_open": False, "shift_number": 6, "receipt_number": 0}


# --- Панель обслуживания: срок действия, версия ФН, фискализация, ОФД ----

def test_разбор_срока_действия_фн_живой_кассы():
    # Спецификация обещает 3 байта, живая касса вернула 5 — два лишних байта
    # не расшифрованы, возвращаются сырьём.
    payload = bytes.fromhex("ff0300" + "1c040e4701")
    k = kkt_with([payload])
    st = k.fn_expiry()
    assert st["expiry"] == "14.04.2028"
    assert st["tail"] == "47 01"


def test_срок_действия_фн_из_трёх_байт_не_ломает_разбор():
    # Ровно то, что обещает спецификация: без хвоста.
    payload = bytes.fromhex("ff0300" + "1c040e")
    k = kkt_with([payload])
    st = k.fn_expiry()
    assert st["expiry"] == "14.04.2028"
    assert st["tail"] == ""


def test_разбор_версии_фн_живой_кассы():
    payload = bytes.fromhex("ff0400" + "666e5f765f315f325f3220202020200001")
    k = kkt_with([payload])
    st = k.fn_version()
    assert st["version"] == "fn_v_1_2_2"
    assert st["serial_software"] is True


def test_разбор_итогов_фискализации_живой_кассы():
    payload = bytes.fromhex(
        "ff0900"
        "19031f0c25"
        "343633323433363233323830"
        "3030303839333632393330303839363820202020"
        "020000010000003bf21e4a"
    )
    k = kkt_with([payload])
    st = k.fiscalization()
    assert st["at"] == "31.03.2025 12:37"
    assert st["inn"] == "463243623280"
    assert st["reg_number"] == "0008936293008968"
    assert st["tax_systems"] == ["УСН доход"]
    assert st["work_modes"] == 0
    assert st["fd"] == 1
    assert st["fp"] == 1243542075


def test_разбор_количества_неподтверждённых_фд_живой_кассы():
    payload = bytes.fromhex("ff3f00" + "0000")
    k = kkt_with([payload])
    assert k.unconfirmed_documents() == 0


# --- Ошибки кассы --------------------------------------------------------

def test_ненулевой_код_ошибки_превращается_в_исключение():
    # Ответ с кодом ошибки 0x67 плюс ответ на попытку узнать её название
    k = kkt_with([b"\xff\x40\x67", b"\x6b\x37"])
    with pytest.raises(shtrih.KKTError) as e:
        k.shift_params()
    assert e.value.code == 0x67


# --- Параметр открытия ФН (FF0Eh) и версии ФФД -----------------------------
#
# Кадры TLV сняты с живой ККТ 25.08.2026, отчёт о регистрации №1:
#   тег 1209 «версия ФФД»     -> B9 04 01 00 02
#   тег 1189 «версия ФФД ККТ» -> A5 04 01 00 02
#   тег 1190 «версия ФФД ФН»  -> A6 04 01 00 04

def test_параметр_регистрации_разбирает_реальные_кадры():
    k = kkt_with([
        b"\xff\x0e\x00" + bytes.fromhex("b904010002"),
        b"\xff\x0e\x00" + bytes.fromhex("a504010002"),
        b"\xff\x0e\x00" + bytes.fromhex("a604010004"),
    ])
    assert k.registration_param(shtrih.TAG_FFD_VERSION) == b"\x02"
    assert k.registration_param(shtrih.TAG_FFD_KKT) == b"\x02"
    assert k.registration_param(shtrih.TAG_FFD_FN) == b"\x04"


def test_ffd_versions_на_реальных_кадрах():
    # ffd_versions() сперва находит номер отчёта перебором (report 1 отвечает,
    # report 2 даёт 0x08), затем перечитывает три тега из найденного отчёта.
    k = kkt_with([
        b"\xff\x0e\x00" + bytes.fromhex("b904010002"),  # перебор: отчёт 1 отвечает
        b"\xff\x0e\x08",                                 # перебор: отчёт 2 -- нет данных
        b"\xff\x0e\x00" + bytes.fromhex("b904010002"),  # 1209 -> current
        b"\xff\x0e\x00" + bytes.fromhex("a504010002"),  # 1189 -> kkt
        b"\xff\x0e\x00" + bytes.fromhex("a604010004"),  # 1190 -> fn
    ])
    assert k.ffd_versions() == {"report": 1, "current": 2, "kkt": 2, "fn": 4}


def test_параметр_регистрации_на_коде_0x08_отдаёт_none():
    k = kkt_with([b"\xff\x0e\x08"])
    assert k.registration_param(shtrih.TAG_FFD_VERSION) is None


def test_параметр_регистрации_на_коде_0x37_поднимает_kkterror():
    k = kkt_with([b"\xff\x0e\x37"])
    with pytest.raises(shtrih.KKTError) as e:
        k.registration_param(shtrih.TAG_FFD_VERSION)
    assert e.value.code == 0x37


def test_last_registration_report_останавливается_на_первом_0x08():
    k = kkt_with([
        b"\xff\x0e\x00" + bytes.fromhex("b904010002"),  # отчёт 1 отвечает
        b"\xff\x0e\x08",                                 # отчёт 2 -- нет данных
    ])
    assert k.last_registration_report() == 1


def test_ff0e_шлёт_номер_тега_двумя_байтами_little_endian():
    k = kkt_with([b"\xff\x0e\x00" + bytes.fromhex("b904010002")])
    k.registration_param(1209)
    frame = frames_sent(k)[0]
    assert frame[2:4] == shtrih.CMD_FN_REG_PARAM
    # DATA = пароль(4) + номер отчёта(1) + тег(2, little-endian)
    assert frame[-3:-1] == bytes([0xB9, 0x04])
