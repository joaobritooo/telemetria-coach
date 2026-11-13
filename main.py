"""
Telemetria Coach — análise de telemetria em tempo real para ACC e Assetto Corsa.

Uso:
    python main.py                 # autodetecta ACC / AC, abre janela própria no monitor secundário
    python main.py --sim acc       # força ACC
    python main.py --demo          # dados simulados (sem jogo)
    python main.py --monitor 2     # escolhe o monitor do painel
    python main.py --browser       # usa o Edge/Chrome em vez da janela própria
    python main.py --no-window     # só o servidor (abra http://localhost:8765 onde quiser)
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import analysis
from analysis import Engine
from broadcast import ACCBroadcastClient, DemoField, Tracker
from sources import DemoSource, SharedMemorySource

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", HERE)                       # arquivos embutidos (web/)
APP_DIR = os.path.dirname(sys.executable) if FROZEN else HERE  # onde ficam os dados
analysis.DATA_DIR = os.path.join(APP_DIR, "dados")
TITLE = "Telemetria Coach"

# Impede que o painel pare de redesenhar quando o jogo está em foco/tela cheia.
CHROMIUM_FLAGS = [
    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,Translate",
    # desenha o painel na CPU: não disputa a placa de vídeo com o jogo (é o que fazia travar ao entrar no carro)
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--autoplay-policy=no-user-gesture-required",
]

tracker = Tracker()
engine = Engine(tracker)
perf = {"frames": 0, "max_gap": 0.0, "since": time.perf_counter(), "page_fps": None, "page_gap": None}


# ---------------------------------------------------------------- desempenho com o jogo em foco
# O Windows 11 coloca apps em segundo plano no "modo eficiência" (EcoQoS): CPU lenta e timers
# atrasados. Com o jogo em foco o painel ficava travando. Aqui o app (e os processos do WebView2)
# saem desse modo e ganham prioridade um pouco acima do normal.
class _PPT(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


def boost_process(handle):
    try:
        k32 = ctypes.windll.kernel32
        st = _PPT(1, 0x1 | 0x4, 0)  # EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION, estado = desligado
        k32.SetProcessInformation(handle, 4, ctypes.byref(st), ctypes.sizeof(st))  # ProcessPowerThrottling
        k32.SetPriorityClass(handle, 0x8000)  # ABOVE_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


def descendants(root_pid):
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_ulong),
                    ("cntThreads", ctypes.c_ulong), ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    parent = {}
    pe = PE()
    pe.dwSize = ctypes.sizeof(PE)
    ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(pe))
    while ok:
        parent[pe.th32ProcessID] = pe.th32ParentProcessID
        ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(pe))
    k32.CloseHandle(ctypes.c_void_p(snap))
    out, todo = set(), {root_pid}
    while todo:
        p = todo.pop()
        kids = {c for c, par in parent.items() if par == p and c not in out and c != p}
        out |= kids
        todo |= kids
    return out


def keep_boosted():
    if sys.platform != "win32":
        return
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    boost_process(ctypes.c_void_p(k32.GetCurrentProcess()))
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)  # sleep() preciso
    except Exception:
        pass
    done = set()
    while True:
        for pid in descendants(os.getpid()) - done:
            h = k32.OpenProcess(0x0200 | 0x0400, False, pid)  # SET_INFORMATION | QUERY_INFORMATION
            if h:
                boost_process(ctypes.c_void_p(h))
                k32.CloseHandle(ctypes.c_void_p(h))
                done.add(pid)
        time.sleep(5)


def poll_loop(source, hz):
    period = 1.0 / hz
    last = time.perf_counter()
    while True:
        t0 = time.perf_counter()
        perf["frames"] += 1
        perf["max_gap"] = max(perf["max_gap"], t0 - last)
        last = t0
        try:
            engine.process(source.read())
        except Exception as e:  # nunca deixa o loop morrer
            print("erro no processamento:", repr(e), flush=True)
            time.sleep(0.5)
        if t0 - perf["since"] > 30:
            el = t0 - perf["since"]
            print(f"[desempenho] leitura {perf['frames'] / el:.0f} Hz, maior pausa {perf['max_gap'] * 1000:.0f} ms | "
                  f"painel {perf['page_fps'] or '?'} fps, maior pausa {perf['page_gap'] or '?'} ms | "
                  f"jogo conectado: {engine.connected}", flush=True)
            perf.update(frames=0, max_gap=0.0, since=t0)
        time.sleep(max(0.0, period - (time.perf_counter() - t0)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/icone.png", "/favicon.ico"):
            name, ctype = ("index.html", "text/html; charset=utf-8") if path in ("/", "/index.html") else ("icone.png", "image/png")
            with open(os.path.join(RES_DIR, "web", name), "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    try:
                        payload = json.dumps(engine.snapshot(), ensure_ascii=False)
                    except Exception as e:
                        print("erro no snapshot:", repr(e), flush=True)
                        time.sleep(0.5)
                        continue
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(1 / 15)
            except (ConnectionError, OSError):
                return
        elif path == "/api/ref":
            self._json(engine.ref_payload())
        elif path == "/api/map":
            self._json(engine.map_payload())
        elif path == "/api/state":
            self._json(engine.snapshot())
        elif path == "/api/history":
            self._json(engine.history_payload())
        elif path == "/api/compare":
            q = parse_qs(urlparse(self.path).query)
            self._json(engine.compare_payload(q.get("a", ["ref"])[0], q.get("b", ["last"])[0]))
        else:
            self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path == "/api/clear_ref":
            engine.clear_reference()
            self._json({"ok": True})
        elif urlparse(self.path).path == "/api/perf":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
                perf["page_fps"], perf["page_gap"] = d.get("fps"), d.get("gap")
            except ValueError:
                pass
            self._json({"ok": True})
        elif urlparse(self.path).path == "/api/quit":
            self._json({"ok": True})
            threading.Timer(0.2, lambda: os._exit(0)).start()
        else:
            self.send_error(404)


class ExclusiveServer(ThreadingHTTPServer):
    """No Windows, o padrão deixa DUAS cópias do app ouvirem a mesma porta, e o painel
    acabava falando com a cópia velha/travada. Aqui a porta é exclusiva."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ---------------------------------------------------------------- monitores
def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # coordenadas reais de pixel
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def list_monitors():
    if sys.platform != "win32":
        return []
    import ctypes.wintypes as wt
    mons = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(hmon, hdc, rect, lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        mons.append({"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                     "primary": bool(mi.dwFlags & 1)})
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, PROC(cb), 0)
    mons.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))  # 1 = principal
    return mons


def pick_monitor(monitor_index):
    mons = list_monitors()
    for i, m in enumerate(mons, 1):
        print(f"  monitor {i}: {m['w']}x{m['h']} em ({m['x']},{m['y']}){' [principal]' if m['primary'] else ''}")
    if not mons:
        return None
    if monitor_index and 1 <= monitor_index <= len(mons):
        return mons[monitor_index - 1]
    return next((m for m in mons if not m["primary"]), mons[0])


def own_window():
    """Janela visível com o título do app que pertence a ESTE processo (o console também pode ter o mesmo título)."""
    import ctypes.wintypes as wt
    u32 = ctypes.windll.user32
    found = []
    pid = os.getpid()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, lp):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == TITLE:
                found.append(hwnd)
        return True

    u32.EnumWindows(cb, 0)
    return found[0] if found else None


# ---------------------------------------------------------------- janela própria (WebView2)
def run_native_window(url, target, fullscreen):
    """Abre o painel numa janela própria. Bloqueia até a janela ser fechada."""
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join(CHROMIUM_FLAGS)
    import webview

    frameless = bool(fullscreen and target)
    win = webview.create_window(TITLE, url, width=1280, height=760, frameless=frameless,
                                easy_drag=False, background_color="#0a0d12", text_select=False)

    def place():
        if not target:
            return
        hwnd = own_window()
        if hwnd:
            if fullscreen:
                x, y, w, h = target["x"], target["y"], target["w"], target["h"]
            else:
                w, h = int(target["w"] * 0.85), int(target["h"] * 0.85)
                x, y = target["x"] + (target["w"] - w) // 2, target["y"] + (target["h"] - h) // 2
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040 | 0x0004)  # SHOWWINDOW|NOZORDER

    win.events.shown += lambda: threading.Timer(0.3, place).start()
    webview.start(gui="edgechromium", private_mode=False,
                  storage_path=os.path.join(analysis.DATA_DIR, "janela"))


def open_browser_window(url, target, fullscreen):
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    exe = next((c for c in cands if os.path.isfile(c)), None)
    if not exe:
        webbrowser.open(url)
        return
    # perfil novo a cada execução: garante que as flags valham (não reaproveita processo antigo)
    profile = os.path.join(analysis.DATA_DIR, "perfil-navegador", str(int(time.time())))
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run"] + CHROMIUM_FLAGS
    if target:
        args += [f"--window-position={target['x']},{target['y']}", f"--window-size={target['w']},{target['h']}"]
        if fullscreen:
            args.append("--start-fullscreen")
    subprocess.Popen(args)


# ---------------------------------------------------------------- programas que abrem junto
SIMHUB_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles(x86)%\SimHub\SimHubWPF.exe"),
    os.path.expandvars(r"%ProgramFiles%\SimHub\SimHubWPF.exe"),
]


def load_config():
    """dados/config.json — completa com os valores padrão na primeira vez."""
    path = os.path.join(analysis.DATA_DIR, "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    changed = False
    if "abrir_junto" not in cfg:
        # na primeira vez coloca o SimHub (que roda o iFlag), se estiver instalado
        cfg["abrir_junto"] = [p for p in SIMHUB_CANDIDATES if os.path.isfile(p)][:1]
        cfg["_ajuda"] = "Programas abertos junto com o Telemetria Coach (se ainda não estiverem abertos). Deixe [] para nenhum."
        changed = True
    if "espelhar_mapa" not in cfg:
        cfg["espelhar_mapa"] = False
        cfg["_ajuda_mapa"] = "Se o mapa aparecer espelhado (pista ao contrário) e o spotter trocar esquerda/direita, mude para true."
        changed = True
    if changed:
        os.makedirs(analysis.DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return cfg


def companion_apps():
    return load_config()["abrir_junto"]


def launch_companions():
    from sources import running_processes
    running = running_processes()
    for exe in companion_apps():
        name = os.path.basename(exe).lower()
        if name in running:
            print(f"{name} já está aberto", flush=True)
            continue
        if not os.path.isfile(exe):
            print(f"não encontrei {exe}", flush=True)
            continue
        # ShellExecute: funciona também se o programa pedir permissão de administrador
        r = ctypes.windll.shell32.ShellExecuteW(None, "open", exe, None, os.path.dirname(exe), 7)  # 7 = minimizado
        print(f"abrindo {exe}: {'ok' if r > 32 else 'falhou (' + str(r) + ')'}", flush=True)


def setup_logging():
    """No .exe sem console, manda as mensagens para dados/log.txt."""
    if sys.stdout is None or FROZEN:
        os.makedirs(analysis.DATA_DIR, exist_ok=True)
        log = open(os.path.join(analysis.DATA_DIR, "log.txt"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Telemetria Coach para ACC / Assetto Corsa")
    ap.add_argument("--sim", choices=["acc", "ac"], help="força o simulador (padrão: autodetecta)")
    ap.add_argument("--demo", action="store_true", help="usa dados simulados")
    ap.add_argument("--demo-speed", type=float, default=1.0, help="acelera o tempo no modo demo")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--hz", type=int, default=60, help="frequência de leitura da telemetria")
    ap.add_argument("--monitor", type=int, default=0, help="monitor do painel (1 = principal). Padrão: o secundário")
    ap.add_argument("--windowed", action="store_true", help="janela normal em vez de tela cheia")
    ap.add_argument("--browser", action="store_true", help="abre no Edge/Chrome em vez da janela própria")
    ap.add_argument("--no-window", action="store_true", help="não abre o painel automaticamente")
    ap.add_argument("--no-companions", action="store_true", help="não abre o SimHub/iFlag junto")
    args = ap.parse_args()
    setup_logging()
    set_dpi_aware()
    try:
        tracker.mirror = bool(load_config().get("espelhar_mapa"))
    except OSError:
        pass

    if args.demo:
        source = DemoSource(args.demo_speed, DemoField(tracker))
    elif sys.platform != "win32":
        sys.exit("A leitura de ACC/AC só funciona no Windows. Use --demo para testar.")
    else:
        source = SharedMemorySource(args.sim)
        if args.sim != "ac":
            ACCBroadcastClient(tracker).start()  # oponentes do ACC (UDP local)

    threading.Thread(target=poll_loop, args=(source, args.hz), daemon=True).start()
    threading.Thread(target=keep_boosted, daemon=True).start()
    if not args.no_companions and not args.demo and sys.platform == "win32":
        threading.Thread(target=launch_companions, daemon=True).start()
    try:
        server = ExclusiveServer(("127.0.0.1", args.port), Handler)
    except OSError:
        ctypes.windll.user32.MessageBoxW(None, f"A porta {args.port} já está em uso.\nO Telemetria Coach já está aberto?", TITLE, 0x10)
        return
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://localhost:{args.port}/"
    print(f"Telemetria Coach rodando em {url}")
    print("Fonte:", "DEMO" if args.demo else (args.sim or "autodetectar ACC/AC"), flush=True)
    fullscreen = not args.windowed

    if not args.no_window and not args.browser:
        try:
            run_native_window(url, pick_monitor(args.monitor), fullscreen)
            return  # fechou a janela = fecha o app
        except Exception as e:
            print("janela própria indisponível, usando o navegador:", repr(e), flush=True)
    if not args.no_window:
        open_browser_window(url, pick_monitor(args.monitor), fullscreen)
    print("Ctrl+C para sair.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
