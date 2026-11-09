#!/bin/bash
#
# Собирает Kassa.app и кладёт его в Программы.
#
# Клик по иконке поднимает сервер и открывает браузер. Повторный клик второй
# сервер не плодит — просто открывает вкладку на том же адресе. Приложение
# остаётся работать в фоне (без значка в Dock), пока работает запущенный им сервер.
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
  <!-- Приложение живёт в фоне, пока жив сервер: значок в Dock не нужен -->
  <key>LSUIElement</key>               <true/>
  <!-- Без этого ключа macOS молча отказывает в доступе к локальной сети
       (ошибка No route to host) и даже не спрашивает разрешения -->
  <key>NSLocalNetworkUsageDescription</key>
  <string>Касса подключается к фискальному регистратору в локальной сети.</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/Resources/kassa-launcher.sh" <<LAUNCHER
#!/bin/bash
# Собран install_app.sh из $PROJECT — правки вносить там, не здесь.
PROJECT="$PROJECT"
PORT="$PORT"
LAUNCHER
cat >> "$APP/Contents/Resources/kassa-launcher.sh" <<'LAUNCHER'
URL="http://127.0.0.1:$PORT"
LOG="$PROJECT/kassa.log"
SERVER_PID=""

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

start_server() {
  cd "$PROJECT"
  nohup "$PROJECT/.venv/bin/python" app.py --port "$PORT" --strict-port >>"$LOG" 2>&1 &
  SERVER_PID=$!
  # Ждём не фиксированные 20 секунд, а пока жив процесс сервера: первый запуск
  # после пересборки бандла стоит на запросах macOS о доступе к диску и к
  # локальной сети, и пока человек не ответил, сервер не стартует. Если
  # приложение погаснет раньше, сервер потом поднимется уже без доступа к сети.
  # Предел 3 минуты — чтобы зависший сервер не держал лаунчер вечно.
  for _ in $(seq 1 360); do
    kill -0 "$SERVER_PID" 2>/dev/null || break
    running && break
    sleep 0.5
  done
}

stale_server() {
  # Нет поля version = сборка старше той, где вообще появилась эта проверка —
  # такой сервер точно устарел, даже если сам он об этом не знает.
  echo "$1" | grep -q '"stale":true' || ! echo "$1" | grep -q '"version"'
}

if running; then
  # Сервер уже отвечает — но код в его памяти мог устареть (например, его
  # никто не перезапускал после git pull). Спрашиваем у него самого.
  ping_body="$(curl --noproxy '*' --max-time 1 "$URL/api/ping" 2>/dev/null || true)"
  if stale_server "$ping_body"; then
    if curl -fsS --noproxy '*' --max-time 2 -X POST "$URL/api/quit" >/dev/null 2>&1; then
      # /api/quit сам откажется гасить кассу посреди обмена (HTTP 409),
      # тогда curl -f вернёт ненулевой код и мы сюда не попадём.
      for _ in $(seq 1 20); do        # ждём до 10 секунд освобождения порта
        running || break
        sleep 0.5
      done
      start_server
    else
      osascript -e "display dialog \"Запущен старый сервер, а касса сейчас занята операцией — перезапуск отложен. Повторите позже кнопкой «Перезапустить кассу» в интерфейсе.\" with title \"Касса\" buttons {\"Ладно\"} default button 1 with icon caution" >/dev/null 2>&1
    fi
  fi
else
  start_server
fi

# Сервер не ответил, но процесс жив (вышли по пределу ожидания): гасим его,
# чтобы не оставлять на порту сироту, которой macOS режет сеть.
if ! running && [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
  kill "$SERVER_PID" 2>/dev/null
fi

running || fail "Касса не поднялась на порту $PORT. Последние строки kassa.log:

$(tail -n 12 "$LOG" 2>/dev/null)"

open "$URL"

# macOS даёт серверу доступ к локальной сети, только пока жив процесс
# приложения, которое его запустило. Без ожидания приложение гаснет сразу после
# открытия браузера, и касса уходит «не в сети» (No route to host). Если сервер
# поднят раньше — не ждём: его держит живым то приложение, что его запустило.
if [ -n "$SERVER_PID" ]; then
  wait "$SERVER_PID"
fi
LAUNCHER

chmod +x "$APP/Contents/Resources/kassa-launcher.sh"

# Главный исполняемый файл бандла — маленькая скомпилированная программа.
# macOS выдаёт разрешение «Локальная сеть» (и вообще спрашивает о нём) только
# скомпилированному приложению; если CFBundleExecutable — bash-скрипт, сеть
# молча режется (ошибка No route to host). Программа лишь запускает
# Resources/kassa-launcher.sh и возвращает его код выхода; скрипт же не выходит,
# пока жив запущенный им сервер, — так приложение остаётся в фоне вместе с ним.
if xcrun --find clang >/dev/null 2>&1; then
  CC_CMD=(xcrun clang)
elif command -v cc >/dev/null 2>&1; then
  CC_CMD=(cc)
else
  echo "Не найден компилятор C: нужны инструменты командной строки Xcode: xcode-select --install" >&2
  exit 1
fi

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

cat > "$BUILD/launcher.c" <<'CSRC'
#include <limits.h>
#include <libgen.h>
#include <mach-o/dyld.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;

int main(void) {
    char raw[PATH_MAX], self[PATH_MAX], dir[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof(raw);

    /* свой путь -> .../Contents/MacOS/kassa-launcher, симлинки раскрыты */
    if (_NSGetExecutablePath(raw, &size) != 0 || !realpath(raw, self)) return 1;

    /* два раза dirname: MacOS -> Contents; скрипт лежит в Contents/Resources */
    strlcpy(dir, self, sizeof(dir));
    strlcpy(dir, dirname(dir), sizeof(dir));
    strlcpy(dir, dirname(dir), sizeof(dir));
    if (snprintf(script, sizeof(script), "%s/Resources/kassa-launcher.sh", dir)
            >= (int)sizeof(script)) return 1;

    char *argv[] = {"/bin/bash", script, NULL};
    pid_t pid;
    if (posix_spawn(&pid, "/bin/bash", NULL, NULL, argv, environ) != 0) return 1;

    int status;
    if (waitpid(pid, &status, 0) < 0 || !WIFEXITED(status)) return 1;
    return WEXITSTATUS(status);
}
CSRC

"${CC_CMD[@]}" -O2 -Wall -o "$APP/Contents/MacOS/kassa-launcher" "$BUILD/launcher.c"

# Подпись ставим последней, когда в бандле уже лежит всё (бинарь и скрипт в
# Resources) — она покрывает оба. Без подписи macOS не заводит приложению запись
# в «Локальной сети». Подпись ad-hoc, поэтому после каждой пересборки macOS
# заново спросит доступ к локальной сети и (если проект на /Volumes) к внешнему
# диску. Упавший codesign остановит скрипт (set -e) — неподписанный бандл не нужен.
codesign --force --sign - "$APP"

touch "$APP"                          # чтобы Finder перечитал иконку

echo "Установлено: $APP"
echo "Адрес интерфейса: http://127.0.0.1:$PORT"
echo "При первом запуске macOS спросит доступ к локальной сети и (если проект на внешнем диске) к тому — разрешите оба, иначе касса будет «не в сети»."
