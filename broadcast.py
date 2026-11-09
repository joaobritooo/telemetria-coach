"""
Oponentes em tempo real.

ACC: usa a Broadcasting API oficial do jogo (UDP). O jogo precisa ter, em
Documentos/Assetto Corsa Competizione/Config/broadcasting.json, um "updListenerPort" > 0
(o padrão é 9000 com senha "asd"). O app lê esse arquivo sozinho.

Layout dos pacotes conferido com implementações abertas do protocolo:
- acbc (Rust)  https://docs.rs/crate/acbc/latest/source/src/parser.rs
- broawp (Go)  https://github.com/geniusdex/broawp (accbroadcast/messageparser.go)
- @pitwall/acc-node-wrapper (npm)

AC (original): o jogo não expõe dados dos outros carros pela shared memory, então
oponentes só estão disponíveis no ACC.
"""
import ctypes
import json
import math
import os
import random
import socket
import struct
import threading
import time
from collections import deque

CP_PER_LAP = 100        # "laços de cronometragem" virtuais por volta para calcular gaps
INT_MAX = 2147483647
LOCATIONS = {0: "?", 1: "pista", 2: "box", 3: "entrada box", 4: "saída box"}
SESSION_TYPES = {0: "Treino", 4: "Classificação", 9: "Superpole", 10: "Corrida", 11: "Hotlap", 12: "Hotstint"}


class Car:
    def __init__(self, idx):
        self.idx = idx
        self.num = idx
        self.team = ""
        self.driver = f"Carro {idx}"
        self.short = ""
        self.cup = 0
        self.position = 0
        self.laps = 0
        self.progress = 0.0
        self.location = 1
        self.kmh = 0
        self.best_ms = None
        self.last_ms = None
        self.cur_ms = None
        self.hist = deque(maxlen=5)   # voltas limpas recentes (ritmo)
        self.cp = {}                  # checkpoint absoluto -> instante
        self.cp_order = deque()
        self.last_cp = None
        self.pits = 0
        self.seen = 0.0
        # posição no mundo (m) e velocidade estimada entre pacotes
        self.x = self.y = None
        self.t_pos = 0.0
        self.vx = self.vy = 0.0
        self.hx = self.hy = None   # direção de movimento (unitária)

    def pos_at(self, t):
        """Posição extrapolada para o instante t (pacotes chegam a cada ~100 ms)."""
        dt = max(0.0, min(0.3, t - self.t_pos))
        return self.x + self.vx * dt, self.y + self.vy * dt

    @property
    def in_pit(self):
        return self.location in (2, 3, 4)

    def pace(self):
        h = list(self.hist)[-3:]
        return sum(h) / len(h) / 1000 if h else None


class Tracker:
    """Estado de todos os carros + cálculo de gaps. Alimentado pelo cliente UDP ou pela demo."""

    def __init__(self):
        self.lock = threading.RLock()
        self.cars = {}
        self.player = None
        self.session = {}
        self.track_m = 0
        self.status = "aguardando"
        self.events = deque(maxlen=50)
        self.updated = 0.0
        self.clock = time.monotonic     # mesmo relógio dos instantes passados em update_car
        self.mirror = False             # espelha mapa e lados do spotter (dados/config.json)
        self.map_n = 800
        self.map_bins = [None] * self.map_n
        self.map_version = 0
        self.map_dirty = 0

    def reset(self):
        with self.lock:
            self.cars.clear()
            self.player = None

    # ------------------------------------------------------------ mapa da pista
    def reset_map(self, pts=None):
        with self.lock:
            self.map_bins = [None] * self.map_n
            if pts and len(pts) == self.map_n:
                self.map_bins = [tuple(p) if p else None for p in pts]
            self.map_version += 1

    def _map_add(self, spline, x, y):
        k = int(spline * self.map_n) % self.map_n
        cur = self.map_bins[k]
        # média de vários carros/voltas = linha central da pista
        self.map_bins[k] = (x, y) if cur is None else (cur[0] * 0.9 + x * 0.1, cur[1] * 0.9 + y * 0.1)
        self.map_dirty += 1
        if self.map_dirty >= 400:
            self.map_dirty = 0
            self.map_version += 1

    def map_coverage(self):
        return sum(1 for p in self.map_bins if p) / self.map_n

    # ------------------------------------------------------------ spotter
    def spotter(self, car_len=4.6, side_m=5.5):
        """Carros ao lado do jogador. Retorna (esquerda, direita, mais_proximo_atras_m)."""
        with self.lock:
            me = self.cars.get(self.player)
            if not me or me.x is None or me.hx is None or me.in_pit:
                return None
            now = self.clock()
            mx, my = me.pos_at(now)
            left = right = False
            for c in self.cars.values():
                if c is me or c.x is None or c.in_pit or now - c.seen > 2:
                    continue
                cx, cy = c.pos_at(now)
                rx, ry = cx - mx, cy - my
                lon = rx * me.hx + ry * me.hy            # >0 = à frente
                lat = me.hx * ry - me.hy * rx            # >0 = direita (mapa visto de cima, y para baixo)
                if self.mirror:
                    lat = -lat
                if abs(lon) < car_len + 1.0 and 0.8 < abs(lat) < side_m:
                    if lat > 0:
                        right = True
                    else:
                        left = True
            return left, right

    def car(self, idx):
        c = self.cars.get(idx)
        if c is None:
            c = self.cars[idx] = Car(idx)
        return c

    def set_entry(self, idx, num, team, drivers, cur, cup):
        with self.lock:
            c = self.car(idx)
            c.num, c.team, c.cup = num, team, cup
            if drivers:
                d = drivers[min(cur, len(drivers) - 1)]
                c.driver = (d[0][:1] + ". " + d[1]).strip(". ") if d[1] else d[0]
                c.short = d[2]

    def update_car(self, idx, t, laps, spline, position, location, kmh, best, last, cur, x=None, y=None):
        with self.lock:
            c = self.car(idx)
            c.seen = t
            self.updated = t
            if x is not None and not (x == 0 and y == 0):
                if c.x is not None and t - c.t_pos > 0.005:
                    dt = t - c.t_pos
                    vx, vy = (x - c.x) / dt, (y - c.y) / dt
                    sp = math.hypot(vx, vy)
                    if sp < 120:  # descarta teleporte (volta ao box etc.)
                        c.vx, c.vy = vx, vy
                        if sp > 3:
                            c.hx, c.hy = vx / sp, vy / sp
                    else:
                        c.vx = c.vy = 0.0
                c.x, c.y, c.t_pos = x, y, t
                if location == 1 and 0 <= spline < 1:
                    self._map_add(spline, x, y)
            prog = laps + spline
            # ao cruzar a linha o spline zera um instante antes do contador de voltas: ignora o "pulo"
            if c.cp and prog < c.progress - 0.5 and c.progress - prog < 1.5:
                prog = c.progress
            if c.last_cp is not None and prog >= c.progress:
                new_cp = int(prog * CP_PER_LAP)
                if new_cp > c.last_cp and new_cp - c.last_cp < 30:
                    t0, p0 = c.cp[c.last_cp], c.progress
                    for k in range(c.last_cp + 1, new_cp + 1):
                        frac = (k / CP_PER_LAP - p0) / max(1e-6, prog - p0)
                        c.cp[k] = t0 + (t - t0) * min(1.0, max(0.0, frac))
                        c.cp_order.append(k)
                    c.last_cp = new_cp
                elif new_cp - c.last_cp >= 30:
                    c.cp.clear(); c.cp_order.clear()
                    c.last_cp = new_cp
                    c.cp[new_cp] = t
                    c.cp_order.append(new_cp)
            elif c.last_cp is None:
                c.last_cp = int(prog * CP_PER_LAP)
                c.cp[c.last_cp] = t
                c.cp_order.append(c.last_cp)
            while len(c.cp_order) > CP_PER_LAP * 3:
                c.cp.pop(c.cp_order.popleft(), None)
            was_pit = c.in_pit
            c.progress, c.laps, c.position, c.location, c.kmh = prog, laps, position, location, kmh
            if not was_pit and c.in_pit and c.seen and location in (2, 3):
                c.pits += 1
                self.events.append(("pit_in", idx, t))
            if was_pit and location == 1:
                self.events.append(("pit_out", idx, t))
            c.best_ms = best.get("ms") or c.best_ms
            c.cur_ms = cur.get("ms")
            lm = last.get("ms")
            if lm and lm != c.last_ms:
                c.last_ms = lm
                if not (last.get("invalid") or last.get("out") or last.get("inlap")):
                    c.hist.append(lm)
                self.events.append(("lap", idx, t))

    # gap em segundos: positivo = "other" está à frente de "me"
    def gap(self, me, other):
        if me is None or other is None or me is other:
            return 0.0
        if other.progress >= me.progress:
            k = me.last_cp
            if k in me.cp and k in other.cp:
                return me.cp[k] - other.cp[k]
        else:
            k = other.last_cp
            if k in me.cp and k in other.cp:
                return -(other.cp[k] - me.cp[k])
        pace = me.pace() or other.pace()
        return (other.progress - me.progress) * pace if pace else None

    def pop_events(self):
        with self.lock:
            ev = list(self.events)
            self.events.clear()
            return ev

    def standings(self):
        with self.lock:
            cars = [c for c in self.cars.values() if self.updated - c.seen < 10]
            cars.sort(key=lambda c: (c.position or 999, -c.progress))
            return cars


# ---------------------------------------------------------------- cliente UDP do ACC
def documents_dir():
    try:
        buf = ctypes.create_unicode_buffer(520)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL (segue o OneDrive)
        if buf.value:
            return buf.value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def read_broadcast_cfg():
    path = os.path.join(documents_dir(), "Assetto Corsa Competizione", "Config", "broadcasting.json")
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None, path
    try:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(raw) > 1 and raw[1] == 0):
            txt = raw.decode("utf-16")
        else:
            txt = raw.decode("utf-8-sig")
        return json.loads(txt), path
    except (ValueError, UnicodeDecodeError):
        return None, path


class _R:
    def __init__(self, b):
        self.b, self.i = b, 1

    def _u(self, fmt, n):
        v = struct.unpack_from(fmt, self.b, self.i)[0]
        self.i += n
        return v

    def u8(self): return self._u("<B", 1)
    def i8(self): return self._u("<b", 1)
    def u16(self): return self._u("<H", 2)
    def i32(self): return self._u("<i", 4)
    def u32(self): return self._u("<I", 4)
    def f32(self): return self._u("<f", 4)

    def s(self):
        n = self.u16()
        v = self.b[self.i:self.i + n].decode("utf-8", "replace")
        self.i += n
        return v

    def lap(self):
        ms = self.i32()
        self.u16(); self.u16()
        splits = [self.i32() for _ in range(self.u8())]
        inv, vfb, out, inl = self.u8(), self.u8(), self.u8(), self.u8()
        return {"ms": ms if 0 < ms < INT_MAX else None, "invalid": bool(inv), "out": bool(out), "inlap": bool(inl),
                "splits": [x if 0 < x < INT_MAX else None for x in splits]}


def _ks(s):
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


class ACCBroadcastClient(threading.Thread):
    def __init__(self, tracker, interval_ms=100):  # 100 ms: o spotter precisa de posições frescas
        super().__init__(daemon=True)
        self.tr = tracker
        self.interval = interval_ms
        self.conn_id = None
        self.sock = None
        self.last_entry_req = 0

    def _send(self, data):
        try:
            self.sock.send(data)
        except OSError:
            pass

    def _request_lists(self):
        if self.conn_id is not None and time.time() - self.last_entry_req > 3:
            self.last_entry_req = time.time()
            self._send(struct.pack("<Bi", 10, self.conn_id))
            self._send(struct.pack("<Bi", 11, self.conn_id))

    def run(self):
        while True:
            cfg, path = read_broadcast_cfg()
            port = int((cfg or {}).get("updListenerPort") or 0)
            if not port:
                self.tr.status = f"Broadcasting do ACC desligado (updListenerPort = 0 em {path})" if cfg else "broadcasting.json do ACC não encontrado"
                time.sleep(10)
                continue
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.settimeout(2)
                self.sock.connect(("127.0.0.1", port))
                self.conn_id = None
                self._send(bytes([1, 4]) + _ks("Telemetria Coach") + _ks(cfg.get("connectionPassword", "")) +
                           struct.pack("<I", self.interval) + _ks(cfg.get("commandPassword", "")))
                last_rx = time.time()
                while time.time() - last_rx < 6:
                    try:
                        data = self.sock.recv(65535)
                    except socket.timeout:
                        continue
                    except OSError:  # porta fechada: jogo não está aberto
                        time.sleep(3)
                        break
                    last_rx = time.time()
                    try:
                        self._handle(data)
                    except (struct.error, IndexError):
                        pass
            except OSError:
                time.sleep(3)
            finally:
                if self.conn_id is not None:
                    self._send(struct.pack("<Bi", 9, self.conn_id))
                try:
                    self.sock.close()
                except Exception:
                    pass
                if self.tr.status.startswith("conectado"):
                    self.tr.status = "sem resposta do ACC — tentando de novo"
            time.sleep(1)

    def _handle(self, data):
        r = _R(data)
        typ = data[0]
        tr = self.tr
        if typ == 1:
            cid, ok, ro, err = r.i32(), r.u8(), r.u8(), r.s()
            if ok:
                self.conn_id = cid
                tr.status = "conectado ao ACC"
                tr.reset()
                self.last_entry_req = 0
                self._request_lists()
            else:
                tr.status = f"ACC recusou a conexão: {err} (confira a senha em broadcasting.json)"
        elif typ == 2:
            ev, si, stype, phase = r.u16(), r.u16(), r.u8(), r.u8()
            st, end = r.f32(), r.f32()
            focus = r.u32()
            r.s(); r.s(); r.s()
            if r.u8():
                r.f32(); r.f32()
            tod, amb, trk, clouds, rain, wet = r.f32(), r.u8(), r.u8(), r.u8(), r.u8(), r.u8()
            with tr.lock:
                tr.session = {"type": SESSION_TYPES.get(stype, str(stype)), "is_race": stype == 10, "phase": phase,
                              "time_ms": st, "left_ms": end, "air": amb, "track": trk, "rain": rain / 10, "wet": wet / 10}
                tr.player = focus
        elif typ == 3:
            idx, drv, dcount, gear = r.u16(), r.u16(), r.u8(), r.u8() - 1
            wx, wy, yaw = r.f32(), r.f32(), r.f32()
            loc, kmh, pos, cup_pos, trk_pos = r.u8(), r.u16(), r.u16(), r.u16(), r.u16()
            spline, laps, delta = r.f32(), r.u16(), r.i32()
            best, last, cur = r.lap(), r.lap(), r.lap()
            if idx not in tr.cars:
                self._request_lists()
            tr.update_car(idx, time.monotonic(), laps, spline, pos, loc, kmh, best, last, cur, wx, wy)
        elif typ == 4:
            r.i32()
            idxs = [r.u16() for _ in range(r.u16())]
            with tr.lock:
                for k in list(tr.cars):
                    if k not in idxs:
                        del tr.cars[k]
        elif typ == 5:
            r.i32(); r.s(); r.i32()
            tr.track_m = r.i32()
        elif typ == 6:
            idx, model, team, num, cup, cur, nat = r.u16(), r.u8(), r.s(), r.i32(), r.u8(), r.u8(), r.u16()
            drivers = []
            for _ in range(r.u8()):
                fn, ln, sn, cat, dnat = r.s(), r.s(), r.s(), r.u8(), r.u16()
                drivers.append((fn, ln, sn))
            tr.set_entry(idx, num, team, drivers, cur, cup)


# ---------------------------------------------------------------- oponentes da demo
class _DemoGeom:
    """Traçado fictício com o mesmo comprimento da pista da demo (4.2 km), para mapa e spotter."""

    def __init__(self, length=4200.0, m=2400):
        pts = []
        for i in range(m):
            u = i / m * 2 * math.pi
            pts.append((700 * math.cos(u) + 170 * math.cos(3 * u + 0.5), 420 * math.sin(u) + 110 * math.sin(2 * u)))
        cum = [0.0]
        for i in range(1, m + 1):
            a, b = pts[i - 1], pts[i % m]
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        k = length / cum[-1]
        self.pts = [(x * k, y * k) for x, y in pts]
        self.cum = [c * k for c in cum]
        self.L = length
        self.m = m

    def at(self, frac, lane=0.0):
        d = (frac % 1.0) * self.L
        lo, hi = 0, self.m
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= d:
                lo = mid
            else:
                hi = mid
        a, b = self.pts[lo], self.pts[(lo + 1) % self.m]
        seg = self.cum[lo + 1] - self.cum[lo]
        f = (d - self.cum[lo]) / seg if seg else 0
        x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        tx, ty = (b[0] - a[0]) / (seg or 1), (b[1] - a[1]) / (seg or 1)
        return x - ty * lane, y + tx * lane  # deslocamento lateral (faixa)


class DemoField:
    """Grid simulado para a demo: 14 carros com ritmos diferentes, cada um faz 1 pit stop."""
    NAMES = ["A. Silva", "M. Rossi", "J. Müller", "L. Martin", "K. Tanaka", "P. Costa", "R. Novak",
             "E. Dubois", "T. Becker", "S. Lopes", "D. Vieira", "N. Hughes", "F. Moretti", "G. Andrade"]

    def __init__(self, tracker, base_lap=84.6):
        self.tr = tracker
        self.base = base_lap
        self.cars = []
        for i, n in enumerate(self.NAMES):
            self.cars.append({"idx": i + 1, "pace": base_lap + random.uniform(-1.6, 2.5), "prog": -0.004 * (i + 1) + 0.02,
                              "pit_lap": random.randint(8, 13), "pit_until": None, "pitted": False, "lap_start": 0.0,
                              "best": None, "last": None})
            tracker.set_entry(i + 1, random.randint(2, 99), "Demo Racing", [(n.split(". ")[0], n.split(". ")[1], n[:3].upper())], 0, 0)
        tracker.set_entry(0, 7, "Você", [("Você", "", "VOC")], 0, 0)
        tracker.player = 0
        tracker.session = {"type": "Corrida", "is_race": True}
        tracker.status = "demo"
        tracker.clock = lambda: self.last_t or 0.0
        self.geo = _DemoGeom()
        for c in self.cars:
            c["phase"] = random.uniform(0, 6.28)
        self.last_t = None

    def step(self, t, player_prog, player_kmh, left_ms):
        dt = 0 if self.last_t is None else t - self.last_t
        self.last_t = t
        allc = []
        for c in self.cars:
            speed = 1.0 / (c["pace"] * random.uniform(0.985, 1.015))
            if c["pit_until"] is not None:
                if t < c["pit_until"]:
                    speed *= 0.25
                else:
                    c["pit_until"] = None
            old = c["prog"]
            c["prog"] += dt * speed
            if int(c["prog"]) > int(old) and old > 0:
                lap_t = (t - c["lap_start"]) * 1000
                c["last"] = lap_t
                c["dirty"] = max(0, c.get("dirty", 0) - 1)
                if not c["pitted"] or int(c["prog"]) != c["pit_lap"] + 1:
                    c["best"] = min(c["best"] or 1e9, lap_t)
                c["lap_start"] = t
                if int(c["prog"]) == c["pit_lap"] and not c["pitted"]:
                    c["pitted"] = True
                    c["pit_until"] = t + 28 / 0.75
                    c["dirty"] = 2  # volta de entrada e de saída não contam no ritmo
            allc.append((c["prog"], c))
        allc.append((player_prog, None))
        allc.sort(key=lambda x: -x[0])
        for pos, (prog, c) in enumerate(allc, 1):
            if c is None:
                x, y = self.geo.at(prog, 1.2 * math.sin(t * 0.25))
                self.tr.update_car(0, t, int(prog), prog % 1, pos, 1, int(player_kmh), {}, {}, {}, x, y)
                continue
            loc = 2 if c["pit_until"] else 1
            x, y = self.geo.at(max(0.0, prog), (-8 if loc == 2 else 3.0 * math.sin(t * 0.2 + c["phase"])))
            self.tr.update_car(c["idx"], t, int(max(0, prog)), max(0.0, prog) % 1, pos, loc, 200,
                               {"ms": c["best"]}, {"ms": c["last"], "inlap": c.get("dirty", 0) > 0}, {}, x, y)
        self.tr.session.update({"left_ms": left_ms})
