#!/usr/bin/env python3
r"""
screen_qa_win.py - Windows-версия: скриншот -> OCR -> LLM по API -> озвучка.

Отличия от версии для Linux (screen_qa_linux.py):
  * путь к tesseract.exe ищется автоматически - на Windows его нет в PATH
  * скриншот: mss + включение DPI-awareness (иначе при масштабе 125/150%
    система отдаёт растянутую картинку и OCR читает заметно хуже)
  * озвучка: SAPI5 через PowerShell вместо espeak-ng/spd-say, без лишних пакетов;
    проигрывание через winsound, запасной вариант - pyttsx3
  * глобальный хоткей работает без прав администратора, Wayland-оговорок нет

Установка:
  1) Tesseract (одно из):
       winget install -e --id UB-Mannheim.TesseractOCR
       choco install tesseract
       установщик: https://github.com/UB-Mannheim/tesseract/wiki
     Языки отмечаются галочками в установщике (Additional language data).
     Вручную: положить *.traineddata в C:\Program Files\Tesseract-OCR\tessdata
  2) pip install mss pillow pytesseract requests pynput

Быстрый выход: три нажатия "=" подряд за секунду, из любого окна.
Клавиша не перехватывается, символ как обычно печатается в активной программе.
Отключить или сменить: --exit-key "" либо --exit-key "<f12>"

Запуск (cmd или PowerShell):
  python screen_qa_win.py                      # висит и ждёт F9, Esc - выход
  python screen_qa_win.py --once
  python screen_qa_win.py --region 600,300,1300,800
  python screen_qa_win.py --monitor 2
  python screen_qa_win.py --image photo.png
  python screen_qa_win.py --text "Which cmd command shows file content?" --no-speech
  python screen_qa_win.py --list-models
  python screen_qa_win.py --timeout 10
  python screen_qa_win.py --test-speech

Запуск без окна консоли: pythonw.exe screen_qa_win.py

Использовать только в разрешённых ситуациях (учёба, свои конспекты, доступность).
Не применять на экзаменах и тестах, где это запрещено правилами.
"""
import argparse
import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import mss
import pytesseract
import requests
from PIL import Image

try:
    import winsound
except ImportError:
    winsound = None

# ===== НАСТРОЙКИ (меняются только здесь) =====
API_KEY = "sk-star-05a802f8305315142ee748482c8e17fc9a3f695e3e71b7f8db35f63ee10743f2"  # ключ ai.starimg.ru
# Переменная окружения OPENAI_API_KEY, если задана, перекрывает это значение.
BASE_URL = "https://ai.starimg.ru/v1"   # Base URL смотрите в личном кабинете ai.starimg.ru
MODEL = "gpt-6-luna"                    # имя модели из кабинета (или: --list-models)
OCR_LANG = "eng"                        # "eng+pol", "eng+rus" и т.д. (нужны *.traineddata)
TESSERACT_EXE = ""                      # путь к tesseract.exe, если автопоиск не справился
SPEECH_RATE = 0                         # скорость SAPI, от -10 до 10
TIMEOUT = 20                            # секунд на ответ API и на OCR (--timeout)
EXIT_KEY = "="                          # быстрый выход: эта клавиша подряд (--exit-key)
EXIT_PRESSES = 3                        # сколько раз нажать
EXIT_WINDOW = 1.0                       # за сколько секунд
# Путь к tesseract можно задать и переменной TESSERACT_CMD.
# ==============================================

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": MODEL, "ollama": "qwen2.5:7b"}
CONFIG = {
    "provider": "openai",
    "model": MODEL,
    "openai_url": BASE_URL.rstrip("/") + "/chat/completions",
    "monitor": 1,
    "timeout": TIMEOUT,
}

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

SYSTEM_PROMPT = (
    "You answer multiple-choice IT questions (Windows paths, cmd.exe, PowerShell, "
    "networking, Linux directories). The options are numbered 1-4 in the order they "
    "appear, whatever labels the screen shows. The text comes from OCR and may contain "
    "small errors. Reply with EXACTLY ONE character and nothing else: the digit 1, 2, 3 "
    "or 4 of the correct option. If you cannot understand the question or cannot "
    "decide, reply with a single minus sign: -  No words, no explanations, no punctuation."
)

# Что произносит озвучка
NUMBER_WORDS = {"1": "one", "2": "two", "3": "three", "4": "four", "-": "minus"}


def normalize_answer(raw: str) -> str:
    """Свести ответ модели к одному символу: 1, 2, 3, 4 или '-'."""
    raw = raw.strip()
    m = re.search(r"[1-4]", raw)
    if m:
        return m.group(0)
    m = re.search(r"\b([A-Da-d])\b", raw)  # если модель всё же ответила буквой
    if m:
        return str("abcd".index(m.group(1).lower()) + 1)
    return "-"


# ---------- Windows-специфика окружения ----------
def enable_dpi_awareness():
    """Без этого на мониторе с масштабом 125/150% скриншот приходит размытым."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def line_buffered_output():
    """Печатать сразу, а не блоками: вывод часто уходит в файл или в tee."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass


def fix_console_encoding():
    """Старый conhost в cp866 спотыкается на русском выводе."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_TESSERACT_READY = False


def bundle_dir() -> str:
    """Каталог с распакованными ресурсами, если программа собрана в один exe.

    PyInstaller кладёт их во временную папку и пишет путь в sys._MEIPASS.
    Пустая строка означает обычный запуск из исходника.
    """
    return getattr(sys, "_MEIPASS", "")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def setup_tesseract(explicit: str = ""):
    """Найти tesseract.exe: аргумент -> TESSERACT_CMD -> вложенный в exe -> PATH -> каталоги."""
    global _TESSERACT_READY
    if _TESSERACT_READY:
        return
    candidates = [explicit, TESSERACT_EXE, os.environ.get("TESSERACT_CMD", "")]

    bundle = bundle_dir()
    if bundle:
        inner = os.path.join(bundle, "tesseract")
        candidates.append(os.path.join(inner, "tesseract.exe"))
        candidates.append(os.path.join(inner, "tesseract"))
        tessdata = os.path.join(inner, "tessdata")
        if os.path.isdir(tessdata):
            # Tesseract 5 ищет языковые файлы по этому пути
            os.environ.setdefault("TESSDATA_PREFIX", tessdata)

    candidates.append(shutil.which("tesseract") or "")
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")):
        if base:
            candidates.append(os.path.join(base, "Tesseract-OCR", "tesseract.exe"))
            candidates.append(os.path.join(base, "Programs", "Tesseract-OCR", "tesseract.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            pytesseract.pytesseract.tesseract_cmd = c
            _TESSERACT_READY = True
            return
    if shutil.which("tesseract"):   # не Windows, но в PATH есть
        _TESSERACT_READY = True
        return
    hint = ("    Собранный exe должен нести tesseract внутри, пересоберите build_win.ps1\n"
            if is_frozen() else
            "      winget install -e --id UB-Mannheim.TesseractOCR\n")
    sys.exit("[!] tesseract не найден.\n" + hint +
             '    либо укажите путь: --tesseract "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"')


# ---------- скриншот ----------
def grab_screen(region=None) -> Image.Image:
    with mss.mss() as sct:
        if region:
            left, top, right, bottom = region
            box = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        else:
            idx = CONFIG["monitor"]        # 0 - все мониторы сразу, 1 - основной
            box = sct.monitors[idx if idx < len(sct.monitors) else 1]
        shot = sct.grab(box)
        return Image.frombytes("RGB", shot.size, shot.rgb)


# ---------- OCR ----------
def ocr(image: Image.Image) -> str:
    setup_tesseract()
    gray = image.convert("L")
    gray = gray.resize((gray.width * 2, gray.height * 2))  # так мелкий текст читается лучше
    try:
        return pytesseract.image_to_string(gray, lang=OCR_LANG, config="--psm 6",
                                           timeout=CONFIG["timeout"]).strip()
    except RuntimeError as e:          # pytesseract так сообщает о своём таймауте
        print(f"[!] OCR не уложился в {CONFIG['timeout']:g} с: {e}")
        return ""


# ---------- LLM ----------
KEY_FILE = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "screen_qa", "api_key")


def _key(env_name: str) -> str:
    """Ключ берётся из переменной окружения, затем из файла, затем из API_KEY."""
    key = os.environ.get(env_name) or API_KEY
    if not key and os.path.isfile(KEY_FILE):
        try:
            key = open(KEY_FILE, encoding="utf-8").read().strip()
        except OSError as e:
            print(f"[!] Не прочитать {KEY_FILE}: {e}")
    if not key:
        sys.exit(f"[!] Нет API-ключа. Любой из вариантов:\n"
                 f"    cmd:         setx {env_name} ваш-ключ\n"
                 f"    PowerShell:  $env:{env_name} = 'ваш-ключ'\n"
                 f"    файл:        {KEY_FILE}\n"
                 f"    либо впишите API_KEY в начале файла")
    return key


def ask_llm(question_text: str) -> str:
    provider, model = CONFIG["provider"], CONFIG["model"]

    if provider == "anthropic":
        r = requests.post(
            ANTHROPIC_URL,
            headers={"x-api-key": _key("ANTHROPIC_API_KEY"),
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 10, "temperature": 0,
                  "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": question_text}]},
            timeout=CONFIG["timeout"],
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()

    if provider == "openai":
        r = requests.post(
            CONFIG["openai_url"],
            headers={"Authorization": f"Bearer {_key('OPENAI_API_KEY')}"},
            json={"model": model,
                  "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                               {"role": "user", "content": question_text}]},
            timeout=CONFIG["timeout"],
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    r = requests.post(
        OLLAMA_URL,
        json={"model": model, "system": SYSTEM_PROMPT, "prompt": question_text,
              "stream": False, "options": {"temperature": 0.1}},
        timeout=CONFIG["timeout"],
    )
    r.raise_for_status()
    return r.json()["response"].strip()


# ---------- озвучка ----------
LEAD_SILENCE = 1.0  # секунд тишины перед словом; если начало всё ещё обрезается - увеличьте

# Голос выбирается английский: на русской Windows по умолчанию активна Irina,
# и короткое "one" она произносит неразборчиво.
PS_SPEAK = """$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = __RATE__
$s.Volume = 100
$v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'en' } | Select-Object -First 1
if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }
__OUTPUT__
$s.Speak('__TEXT__')
$s.Dispose()
"""


def _powershell():
    builtin = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(builtin):
        return builtin
    return shutil.which("powershell") or shutil.which("pwsh")


def _run_ps(script: str, timeout: int = 20):
    """Выполнить скрипт PowerShell из временного файла, без мелькающего окна."""
    exe = _powershell()
    if not exe:
        raise RuntimeError("powershell.exe не найден")
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8-sig") as f:
            f.write(script)
            path = f.name
        return subprocess.run([exe, "-NoProfile", "-NonInteractive",
                               "-ExecutionPolicy", "Bypass", "-File", path],
                              capture_output=True, text=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def _ps_script(text: str, wav_path: str = "") -> str:
    output = (f"$s.SetOutputToWaveFile('{wav_path}')" if wav_path
              else "$s.SetOutputToDefaultAudioDevice()")
    return (PS_SPEAK.replace("__RATE__", str(int(SPEECH_RATE)))
                    .replace("__OUTPUT__", output)
                    .replace("__TEXT__", text))


def _pad_wav(src: str, dst: str, lead: float, tail: float):
    """Скопировать wav, добавив тишину в начало и в конец."""
    import wave
    with wave.open(src, "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    frame_bytes = params.sampwidth * params.nchannels
    silence = lambda sec: b"\x00" * (int(params.framerate * sec) * frame_bytes)
    with wave.open(dst, "wb") as out:
        out.setparams(params)
        out.writeframes(silence(lead) + frames + silence(tail))


def speak(text: str):
    """Озвучить короткое английское слово. Пробует SAPI через wav, SAPI напрямую,
    затем pyttsx3, и печатает причину, если ни один способ не сработал."""
    text = re.sub(r"[^A-Za-z ]", "", text).strip()   # в кавычки PowerShell попадает только это
    if not text:
        return
    errors = []

    # SAPI -> wav -> тишина в начале -> winsound.
    # Тишина нужна для Bluetooth-наушников: они "просыпаются" ~1 с и съедают
    # начало звука, а короткое "one" целиком помещается в эту дыру.
    if winsound and _powershell():
        raw = padded = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                raw = f.name
            padded = raw + ".pad.wav"
            r = _run_ps(_ps_script(text, raw))
            if r.returncode == 0 and os.path.getsize(raw) > 44:
                _pad_wav(raw, padded, LEAD_SILENCE, 0.3)
                print(f"[звук] SAPI+winsound: {text}")
                winsound.PlaySound(padded, winsound.SND_FILENAME)
                return
            errors.append(f"SAPI->wav: код {r.returncode} {r.stderr.strip()[:200]}")
        except Exception as e:
            errors.append(f"SAPI->wav: {e}")
        finally:
            for p in (raw, padded):
                if p and os.path.exists(p):
                    os.unlink(p)

    # SAPI сразу на звуковое устройство, без подкладывания тишины.
    if _powershell():
        try:
            r = _run_ps(_ps_script(text))
            if r.returncode == 0:
                print(f"[звук] SAPI: {text}")
                return
            errors.append(f"SAPI: код {r.returncode} {r.stderr.strip()[:200]}")
        except Exception as e:
            errors.append(f"SAPI: {e}")

    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        return
    except Exception as e:
        errors.append(f"pyttsx3: {e}")

    print("[!] Озвучка не сработала. Проверьте звук командой в PowerShell:\n"
          "    Add-Type -AssemblyName System.Speech; "
          "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('one two three')")
    for err in errors:
        print("    " + err)


# ---------- основной поток ----------
def process(text: str, do_speak: bool):
    if len(text) < 10:
        print("[!] Текст не распознан или слишком короткий")
        return
    print("\n--- Распознанный текст ---\n" + text)
    try:
        answer = ask_llm(text)
    except requests.Timeout:
        print(f"[!] API не ответил за {CONFIG['timeout']:g} с. "
              f"Увеличьте --timeout или проверьте сеть")
        return
    except requests.RequestException as e:
        print(f"[!] Ошибка запроса к API: {e}")
        if getattr(e, "response", None) is not None:
            print("    Ответ сервера:", e.response.text[:500])
        return
    answer = normalize_answer(answer)
    print("\n--- Ответ модели ---\n" + answer + "\n")
    if do_speak:
        speak(NUMBER_WORDS[answer])


def run_hotkey_loop(region, do_speak: bool, hotkey: str, exit_key: str = EXIT_KEY):
    """Ждать нажатия горячей клавиши и обрабатывать экран в отдельном потоке.

    Работу нельзя делать прямо в callback: на Windows pynput вызывает его из
    низкоуровневого хука клавиатуры (WH_KEYBOARD_LL), а Windows молча снимает
    хук, если тот не вернул управление за LowLevelHooksTimeout (по умолчанию
    300 мс). OCR с запросом к API занимают секунды, поэтому синхронный
    обработчик срабатывает один раз, а дальше клавиша перестаёт отвечать.
    """
    try:
        from pynput import keyboard
    except ImportError:
        sys.exit("[!] Для горячей клавиши нужен pynput:  pip install pynput\n"
                 "    либо запускайте разово: python screen_qa_win.py --once")

    setup_tesseract()        # проверить OCR сразу, а не на первом нажатии
    busy = threading.Event()
    presses = []

    def on_exit_key():
        """Выход по быстрой серии нажатий, не отходя от чужого окна.

        Клавиша не перехватывается: pynput слушает без suppress, поэтому
        символ как обычно уходит в активное приложение.
        """
        now = time.monotonic()
        presses.append(now)
        del presses[:-EXIT_PRESSES]          # держать только последние N
        if len(presses) == EXIT_PRESSES and now - presses[0] <= EXIT_WINDOW:
            print(f"\n[{exit_key}] нажата {EXIT_PRESSES} раза за "
                  f"{now - presses[0]:.2f} с, выходим.")
            sys.stdout.flush()
            os._exit(0)

    def job():
        try:
            process(ocr(grab_screen(region)), do_speak)
        except Exception as e:
            print(f"[!] Ошибка при обработке: {e}")
        finally:
            busy.clear()
            print(f"Жду {hotkey}...")

    def on_hotkey():
        if busy.is_set():
            print("[i] Предыдущий запрос ещё выполняется, нажмите позже")
            return
        busy.set()
        print(f"\n[{time.strftime('%H:%M:%S')}] {hotkey}: снимаю экран...")
        threading.Thread(target=job, daemon=True).start()

    binds = {hotkey: on_hotkey, "<esc>": lambda: os._exit(0)}
    if exit_key:
        try:
            keyboard.HotKey.parse(exit_key)   # проверить запись до запуска слушателя
            binds[exit_key] = on_exit_key
        except ValueError as e:
            print(f"[!] Клавиша выхода {exit_key!r} не распознана: {e}")
            exit_key = ""

    print(f"Готово. {hotkey} - скриншот и ответ, Esc - выход.")
    if exit_key:
        print(f"Быстрый выход: {exit_key} {EXIT_PRESSES} раза подряд "
              f"за {EXIT_WINDOW:g} с.")
    print(f"Жду {hotkey}...")
    with keyboard.GlobalHotKeys(binds) as h:
        h.join()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", help="left,top,right,bottom")
    p.add_argument("--monitor", type=int, default=1, help="номер монитора, 0 - все сразу")
    p.add_argument("--image", help="путь к картинке вместо скриншота")
    p.add_argument("--text", help="готовый текст вопроса, без OCR")
    p.add_argument("--no-speech", action="store_true")
    p.add_argument("--test-speech", action="store_true", help="только проверить озвучку и выйти")
    p.add_argument("--once", action="store_true", help="один запрос по скриншоту и выход")
    p.add_argument("--delay", type=float, default=0, help="пауза в секундах перед скриншотом")
    p.add_argument("--hotkey", default="<f9>", help="формат pynput, например <f9> или <ctrl>+<alt>+q")
    p.add_argument("--tesseract", default="", help="путь к tesseract.exe")
    p.add_argument("--exit-key", default=EXIT_KEY,
                   help=f"клавиша быстрого выхода, {EXIT_PRESSES} нажатия за "
                        f"{EXIT_WINDOW:g} с (пустая строка - выключить)")
    p.add_argument("--timeout", type=float, default=TIMEOUT,
                   help=f"секунд на ответ API и на OCR (по умолчанию {TIMEOUT})")
    p.add_argument("--provider", choices=["anthropic", "openai", "ollama"], default="openai")
    p.add_argument("--model", help="имя модели (по умолчанию зависит от провайдера)")
    p.add_argument("--openai-url", default=CONFIG["openai_url"], help="для OpenAI-совместимых API")
    p.add_argument("--list-models", action="store_true", help="показать модели сервиса и выйти")
    args = p.parse_args()

    line_buffered_output()
    fix_console_encoding()
    enable_dpi_awareness()
    if sys.platform != "win32":
        print("[!] Это версия для Windows. На Linux запускайте screen_qa_linux.py")

    if args.test_speech:
        for w in ("one", "two", "three", "four", "minus"):
            speak(w)
        return

    CONFIG["provider"] = args.provider
    CONFIG["model"] = args.model or DEFAULT_MODELS[args.provider]
    CONFIG["openai_url"] = args.openai_url
    CONFIG["monitor"] = max(0, args.monitor)
    CONFIG["timeout"] = max(1.0, args.timeout)
    if args.tesseract:
        setup_tesseract(args.tesseract)

    if args.list_models:
        r = requests.get(BASE_URL.rstrip("/") + "/models",
                         headers={"Authorization": f"Bearer {_key('OPENAI_API_KEY')}"},
                         timeout=CONFIG["timeout"])
        print(r.status_code)
        print(r.text[:3000])
        return

    region = tuple(map(int, args.region.split(","))) if args.region else None
    do_speak = not args.no_speech

    if args.text:
        return process(args.text, do_speak)
    if args.image:
        return process(ocr(Image.open(args.image)), do_speak)
    if args.once:
        if args.delay:
            print(f"Скриншот через {args.delay:g} с...")
            time.sleep(args.delay)
        return process(ocr(grab_screen(region)), do_speak)

    run_hotkey_loop(region, do_speak, args.hotkey, args.exit_key)


if __name__ == "__main__":
    sys.exit(main())
