#!/bin/bash
#
# Собирает Kassa.app и кладёт его в Программы.
#
# Клик по иконке поднимает сервер и открывает браузер. Повторный клик второй
# сервер не плодит — просто открывает вкладку на том же адресе.
#
#     ./packaging/install_app.sh
#
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${KASSA_PORT:-8765}"

# /Applications пишется не на всякой машине — тогда кладём в домашние Программы
if [ -w /Applications ]; then
  DEST="/Applications"
else
  DEST="$HOME/Applications"
  mkdir -p "$DEST"
fi

APP="$DEST/Касса.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$PROJECT/packaging/Kassa.icns" "$APP/Contents/Resources/Kassa.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>Касса</string>
  <key>CFBundleDisplayName</key>       <string>Касса</string>
  <key>CFBundleExecutable</key>        <string>kassa-launcher</string>
  <key>CFBundleIdentifier</key>        <string>ru.local.kassa</string>
  <key>CFBundleIconFile</key>          <string>Kassa.icns</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key>    <string>11.0</string>
  <!-- Иконка запускает сервер и гаснет: держать её в Dock незачем -->
  <key>LSUIElement</key>               <true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/kassa-launcher" <<LAUNCHER
#!/bin/bash
# Собран install_app.sh из $PROJECT — правки вносить там, не здесь.
PROJECT="$PROJECT"
PORT="$PORT"
LAUNCHER
cat >> "$APP/Contents/MacOS/kassa-launcher" <<'LAUNCHER'
URL="http://127.0.0.1:$PORT"
LOG="$PROJECT/kassa.log"

fail() {
  osascript -e "display dialog \"$1\" with title \"Касса\" buttons {\"Ладно\"} default button 1 with icon stop" >/dev/null 2>&1
  exit 1
}

[ -d "$PROJECT" ] || fail "Папка проекта не найдена: $PROJECT. Если проект на внешнем диске, подключите его."

# Первый запуск: поднимаем окружение сами, чтобы не гонять человека в терминал
if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  /usr/bin/python3 -m venv "$PROJECT/.venv" >>"$LOG" 2>&1 \
    || fail "Не удалось создать окружение Python. Подробности в kassa.log."
  "$PROJECT/.venv/bin/pip" install -q -r "$PROJECT/requirements.txt" >>"$LOG" 2>&1 \
    || fail "Не удалось поставить зависимости. Подробности в kassa.log."
fi

# --noproxy: в окружении может стоять HTTP_PROXY, и тогда запрос на 127.0.0.1
# уедет в прокси, а свой же сервер окажется «недоступен».
running() {
  curl -fsS --noproxy '*' --max-time 1 "$URL/api/ping" 2>/dev/null \
    | grep -q '"app":"kassa"'
}

if ! running; then
  cd "$PROJECT"
  nohup "$PROJECT/.venv/bin/python" app.py --port "$PORT" --strict-port >>"$LOG" 2>&1 &
  for _ in $(seq 1 40); do            # ждём до 20 секунд
    running && break
    sleep 0.5
  done
fi

running || fail "Касса не поднялась на порту $PORT. Последние строки kassa.log:

$(tail -n 12 "$LOG" 2>/dev/null)"

open "$URL"
LAUNCHER

chmod +x "$APP/Contents/MacOS/kassa-launcher"
touch "$APP"                          # чтобы Finder перечитал иконку

echo "Установлено: $APP"
echo "Адрес интерфейса: http://127.0.0.1:$PORT"
