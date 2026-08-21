"""
Рисует иконку приложения и собирает Kassa.icns.

Запускается руками при изменении иконки, результат коммитится:
Pillow нужен только здесь и в рантайм-зависимости не попадает.

    .venv/bin/python tools/make_icon.py

Палитра — та же, что в интерфейсе: латунь на тёмном графите, белая лента.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "packaging"

GROUND = (14, 21, 25)         # --ground
PANEL = (31, 45, 53)          # --panel-hi
BRASS = (201, 151, 63)        # --brass
PAPER = (242, 237, 225)       # --paper
INK = (42, 38, 34)            # --ink
DIM = (107, 130, 145)         # --dim
LINE = (38, 53, 62)           # --line
OK = (90, 158, 118)           # --ok

S = 1024                      # итоговый размер
K = 4                         # надрисовываем крупнее и ужимаем — так гладко


def squircle(draw: ImageDraw.ImageDraw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def render() -> Image.Image:
    size = S * K
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Корпус: скруглённый квадрат macOS с полями, как у системных иконок
    pad = int(size * 0.085)
    radius = int(size * 0.225)
    squircle(d, (pad, pad, size - pad, size - pad), radius, GROUND)

    # Приборная панель сверху и отбивка под ней — как левая колонка интерфейса
    band = int(size * 0.155)
    d.rounded_rectangle((pad, pad, size - pad, pad + band + radius),
                        radius=radius, fill=PANEL)
    d.rectangle((pad, pad + band, size - pad, pad + band + radius), fill=GROUND)
    d.rectangle((pad, pad + band, size - pad, pad + band + int(size * 0.007)),
                fill=LINE)

    # Лампа связи и подпись рядом — строка «● касса на связи»
    lamp_r = int(size * 0.021)
    cy = pad + band // 2
    cx = pad + int(size * 0.115)

    # Свечение лампы делаем размытием, а не вторым кругом: жёсткий круг
    # читается как обводка, а не как свет.
    halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    spread = int(lamp_r * 2.2)
    hd.ellipse((cx - spread, cy - spread, cx + spread, cy + spread),
               fill=OK + (150,))
    halo = halo.filter(ImageFilter.GaussianBlur(int(lamp_r * 1.1)))

    # Свет обрезаем по корпусу, иначе размытие вылезает за скруглённый край
    body = Image.new("L", (size, size), 0)
    ImageDraw.Draw(body).rounded_rectangle(
        (pad, pad, size - pad, size - pad), radius=radius, fill=255)
    halo.putalpha(Image.composite(halo.getchannel("A"),
                                  Image.new("L", (size, size), 0), body))
    img.alpha_composite(halo)

    d.ellipse((cx - lamp_r, cy - lamp_r, cx + lamp_r, cy + lamp_r), fill=OK)
    bar_h = int(size * 0.016)
    d.rounded_rectangle(
        (cx + int(size * 0.055), cy - bar_h // 2,
         cx + int(size * 0.055) + int(size * 0.27), cy + bar_h // 2),
        radius=bar_h // 2, fill=DIM)

    # Чековая лента
    tape_x0 = int(size * 0.225)
    tape_x1 = size - tape_x0
    tape_y0 = pad + band + int(size * 0.060)
    tape_y1 = size - pad - int(size * 0.095)
    d.rectangle((tape_x0, tape_y0, tape_x1, tape_y1), fill=PAPER)

    # Зубчатый обрыв ленты внизу
    teeth = 9
    step = (tape_x1 - tape_x0) / teeth
    depth = int(size * 0.030)
    for i in range(teeth):
        x0 = tape_x0 + i * step
        d.polygon(
            [(x0, tape_y1), (x0 + step / 2, tape_y1 + depth), (x0 + step, tape_y1)],
            fill=PAPER,
        )

    # Строки чека: шапка, позиции с суммами справа, итог латунью
    line_x0 = tape_x0 + int(size * 0.048)
    line_x1 = tape_x1 - int(size * 0.048)
    span = line_x1 - line_x0
    y = tape_y0 + int(size * 0.058)
    h = int(size * 0.019)
    gap = int(size * 0.047)

    def bar(x0, width, colour, thickness):
        d.rounded_rectangle((x0, y, x0 + width, y + thickness),
                            radius=thickness // 2, fill=colour)

    def header(ratio):
        nonlocal y
        w = int(span * ratio)
        bar(line_x0 + (span - w) // 2, w, INK, int(h * 1.4))
        y += gap

    def rule():
        nonlocal y
        bar(line_x0, span, DIM, max(2, int(h * 0.4)))
        y += int(gap * 0.85)

    def item(name_ratio, sum_ratio):
        """Наименование слева, сумма справа — как на настоящей ленте."""
        nonlocal y
        bar(line_x0, int(span * name_ratio), INK, h)
        w = int(span * sum_ratio)
        bar(line_x1 - w, w, INK, h)
        y += gap

    header(0.60)
    y += int(gap * 0.18)
    rule()
    item(0.44, 0.24)
    item(0.34, 0.20)
    item(0.40, 0.26)
    rule()

    # Итог: слева слово, справа сумма — обе латунью и толще остальных
    total_h = int(h * 1.8)
    bar(line_x0, int(span * 0.28), BRASS, total_h)
    w = int(span * 0.34)
    bar(line_x1 - w, w, BRASS, total_h)

    return img.resize((S, S), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    icon = render()

    png = OUT / "icon.png"
    icon.save(png)

    iconset = OUT / "Kassa.iconset"
    if iconset.exists():
        for f in iconset.iterdir():
            f.unlink()
    iconset.mkdir(exist_ok=True)

    # Набор размеров, который ждёт iconutil
    for px in (16, 32, 64, 128, 256, 512, 1024):
        icon.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{px}x{px}.png")
        if px >= 32:
            icon.resize((px, px), Image.LANCZOS).save(
                iconset / f"icon_{px // 2}x{px // 2}@2x.png")

    icns = OUT / "Kassa.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                   check=True)
    for f in iconset.iterdir():
        f.unlink()
    iconset.rmdir()

    print(f"готово: {icns.relative_to(BASE)} и {png.relative_to(BASE)}")


if __name__ == "__main__":
    sys.exit(main())
