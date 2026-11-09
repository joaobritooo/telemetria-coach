"""
Motor de análise: transforma frames de telemetria em feedback de pilotagem, dicas de setup
e estratégia de corrida.

- Delta ao vivo contra a melhor volta válida (salva em disco por carro+pista).
- Detecção automática das curvas a partir da volta de referência.
- Avaliação curva a curva logo na saída de cada curva, por FASES da curva (ver CONCEITOS).
- Eventos ao vivo: travamento de roda, patinagem, freio+acelerador juntos.
- Equilíbrio estimado (sub/sobresterço) por fase e faixa de velocidade.
- Assistente de setup: pressões, temperaturas, camber, freios, equilíbrio, TC/ABS.
- Corrida: combustível até o fim, economia (lift & coast), perda no pit, retorno na pista
  após o pit, duelos com o carro da frente/de trás (oponentes via broadcast.py).

CONCEITOS DE PILOTAGEM USADOS (fontes de coaching reais):
- Fases da curva (Driver61): frenagem, trail braking, transição de pedais, acelerador
  equilibrado (~10–20%, só mantém a velocidade), aceleração crescente, acelerador total.
  https://driver61.com/uni/corner-phases/
- Rolar (sem freio e sem acelerador) entre soltar o freio e acelerar é NORMAL: uma pausa
  curta para o carro rotacionar. Tamanho depende da curva: ~meio carro em grampos, alguns
  carros em curvas rápidas. Rolar entre o acelerador e o freio (tirar o pé antes de frear)
  só se justifica em casos especiais. (Dion von Moltke / Blayze)
  https://blayze.io/blog/car-racing/coasting-on-the-track
- Erro mais comum: acelerar cedo demais, entre o turn-in e o ápice, antes de conseguir
  começar a abrir o volante — e depois ter que aliviar. (von Moltke, NASA Speed News)
  https://nasaspeed.news/columns/driver-instruction/reapplying-the-throttle-at-the-right-time/
- Lift & coast: técnica de economia de combustível — tirar o pé antes do ponto de frenagem
  no fim das retas, rolar, e frear um pouco mais tarde/leve, mantendo a velocidade de ápice.
  https://coachdaveacademy.com/tutorials/mastering-lift-and-coast-how-to-save-fuel-without-sacrificing-speed/
"""
import json
import math
import os
import re
import statistics
import threading
import time
from collections import deque

# ---------------------------------------------------------------- limites (ajuste à vontade)
CFG = {
    # ACC GT3/GT4 (pneus atuais): pressão QUENTE ideal em psi
    "acc_dry_psi": (26.0, 27.0),
    "acc_wet_psi": (29.5, 31.0),
    "ac_psi": (25.0, 30.0),               # AC varia muito por carro: faixa genérica só para alerta
    "core_temp": (70.0, 95.0),            # °C janela de temperatura do pneu
    "camber_spread": (3.0, 9.0),          # °C interno - externo (só AC tem I/M/O)
    "brake_temp_front": (300.0, 750.0),   # °C durante frenagens
    "brake_temp_rear": (250.0, 700.0),
    "balance_under": 0.12,                # índice > isso = subesterço
    "balance_over": -0.08,                # índice < isso = sobresterço
    "lock_ratio": 0.82,                   # roda girando <82% da velocidade do carro = travando
    "spin_ratio": 1.12,                   # roda traseira >112% = patinando
    # Rolagem (coasting) na transição freio -> acelerador, em comprimentos de carro (~4.6 m).
    # Coaches citam ~0.5 carro em grampos e "alguns carros" em curvas rápidas; aqui damos folga
    # antes de reclamar, e sempre comparamos com a sua própria melhor volta.
    "car_len_m": 4.6,
    "coast_ok_len": {"lenta": 1.5, "media": 3.0, "rapida": 5.0},
    "coast_vs_ref_s": 0.15,               # rolou isso a mais que na referência = perda
    "entry_lift_s": 0.3,                  # tirar o pé antes de frear por mais que isso
    # Estratégia
    "pit_loss_s": 25.0,                   # perda estimada no pit até medir de verdade
    "fuel_margin_laps": 1.0,              # margem de segurança de combustível
    "fuel_save_max_pct": 0.06,            # até 6% de déficit: dá para economizar sem parar
}

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_") or "x"


def fmt_ms(ms):
    if not ms or ms <= 0:
        return "--:--.---"
    m, s = divmod(ms / 1000.0, 60)
    return f"{int(m)}:{s:06.3f}"


def fmt_clock(s):
    s = max(0, int(s))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _fill(arr):
    idx = [i for i, v in enumerate(arr) if v is not None]
    if not idx:
        return None
    out = arr[:]
    for i in range(idx[0]):
        out[i] = arr[idx[0]]
    for i in range(idx[-1] + 1, len(arr)):
        out[i] = arr[idx[-1]]
    for a, b in zip(idx, idx[1:]):
        if b - a > 1:
            for i in range(a + 1, b):
                out[i] = arr[a] + (arr[b] - arr[a]) * (i - a) / (b - a)
    return out


def _v(arr, i):
    """valor do bin i, ou o vizinho mais próximo gravado (volta atual tem buracos)."""
    if arr is None or i < 0 or i >= len(arr):
        return None
    if arr[i] is not None:
        return arr[i]
    for d in (1, -1, 2, -2, 3, -3):
        j = i + d
        if 0 <= j < len(arr) and arr[j] is not None:
            return arr[j]
    return None


def corner_class(min_kmh):
    return "lenta" if min_kmh < 100 else ("media" if min_kmh < 160 else "rapida")


def pass_phases(d, c, bin_m):
    """
    Divide uma passagem pela curva nas fases: frenagem -> soltura do freio -> rolagem
    -> acelerador. d = dict com listas t, speed, gas, brake, steer (podem ter None).
    """
    t, sp, gas, brk, st = d["t"], d["speed"], d["gas"], d["brake"], d.get("steer")
    s0 = max(0, c.start - int(250 / bin_m))
    out = {"brake_on": None, "brake_off": None, "gas_on": None, "coast_s": 0.0, "coast_m": 0.0,
           "entry_lift_s": 0.0, "hesitation": False, "early": False, "abrupt": False}
    g = lambda k: _v(gas, k) or 0.0
    b = lambda k: _v(brk, k) or 0.0
    for k in range(s0, c.apex + 1):
        if b(k) > 0.1:
            out["brake_on"] = k
            break
    ref_point = None
    if out["brake_on"] is not None:
        last = out["brake_on"]
        for k in range(out["brake_on"], min(c.end, c.apex + int(80 / bin_m)) + 1):
            if b(k) > 0.05:
                last = k
        out["brake_off"] = last + 1
        ref_point = last + 1
        # tirou o pé antes de frear? (rolagem acelerador -> freio)
        lift = 0.0
        for k in range(s0, out["brake_on"]):
            if g(k) < 0.05 and b(k) < 0.03 and (_v(sp, k) or 0) > 80:
                t0, t1 = _v(t, k), _v(t, k + 1)
                if t0 is not None and t1 is not None and t1 > t0:
                    lift += (t1 - t0) / 1000
        out["entry_lift_s"] = lift
    else:
        # curva de "lift": sem freio — começa quando o acelerador cai
        for k in range(s0, c.apex + 1):
            if g(k) < 0.5:
                ref_point = k
                break
    if ref_point is None:
        return out
    for k in range(ref_point, c.end + 1):
        if g(k) > 0.2 and g(k + 1) > 0.15 and g(k + 2) > 0.15:
            out["gas_on"] = k
            break
    end = out["gas_on"] if out["gas_on"] is not None else c.end
    for k in range(ref_point, end):
        if g(k) < 0.05 and b(k) < 0.03:
            t0, t1 = _v(t, k), _v(t, k + 1)
            if t0 is not None and t1 is not None and t1 > t0:
                out["coast_s"] += (t1 - t0) / 1000
                out["coast_m"] += bin_m
    if out["gas_on"] is not None:
        k0 = out["gas_on"]
        peak = 0.0
        for k in range(k0, c.end + 1):
            peak = max(peak, g(k))
            if peak > 0.25 and g(k) < 0.05:
                out["hesitation"] = True  # acelerou e teve que tirar de novo
                break
        if st is not None and k0 < c.apex:
            s_k0 = abs(_v(st, k0) or 0)
            s_max = max(abs(_v(st, k) or 0) for k in range(k0, c.apex + 1))
            out["early"] = s_max > s_k0 * 1.15 + 0.01  # ainda estava aumentando o volante
        t0 = _v(t, k0)
        for k in range(k0, min(c.end, k0 + int(40 / bin_m)) + 1):
            if g(k) > 0.9:
                t1 = _v(t, k)
                out["abrupt"] = t0 is not None and t1 is not None and (t1 - t0) < 150
                break
    return out


def brake_stats(d, c, bin_m):
    """Ponto de frenagem, pico de pressão e tempo até o pico numa passagem."""
    br, t = d["brake"], d["t"]
    lo = max(0, c.start - int(150 / bin_m))
    bp = next((k for k in range(lo, c.apex + 1) if (_v(br, k) or 0) > 0.1), None)
    if bp is None:
        return None
    seg = [(_v(br, k) or 0) for k in range(bp, c.apex + 1)]
    peak = max(seg)
    kpk = bp + next(i for i, x in enumerate(seg) if x >= 0.9 * peak)
    t0, t1 = _v(t, bp), _v(t, kpk)
    return {"bp": bp, "peak": peak, "rise_ms": (t1 - t0) if t0 is not None and t1 is not None else None}


# Dicas de pilotagem quando o carro sai de frente / de traseira, por fase da curva.
SLIDE_TIPS = {
    ("under", "entrada"): "saindo de frente na entrada — freie mais em linha reta e solte o freio devagar enquanto vira (trail braking); menos volante",
    ("under", "meio"): "saindo de frente no meio — entrou rápido ou com volante demais; tire um pouco de velocidade e espere o carro girar",
    ("under", "saida"): "saindo de frente na saída — acelerou forte cedo; abra o volante antes de pisar fundo",
    ("over", "entrada"): "traseira escapando na entrada — solte o freio mais suave e reduza marcha sem trancar (freio-motor)",
    ("over", "meio"): "traseira escapando no meio — não tire o pé de uma vez; mantenha acelerador leve (10–20%)",
    ("over", "saida"): "traseira escapando na saída — acelere progressivo e só pise fundo com o volante abrindo",
}


class Lap:
    def __init__(self, number, n, clean_start, fuel):
        self.number = number
        self.n = n
        self.clean = clean_start
        self.t = [None] * n
        self.speed = [None] * n
        self.gas = [None] * n
        self.brake = [None] * n
        self.gear = [None] * n
        self.steer = [None] * n
        self.valid = True
        self.pit = False
        self.fuel_start = fuel
        self.events = []  # (bin, kind)
        self.abs_bins = set()
        self.tc_bins = set()
        self.coast_s = 0.0
        self.sector_marks = {}
        self.ms = 0
        self.corner_dt = {}

    def record(self, b, f):
        self.t[b] = f["cur_ms"]
        self.speed[b] = f["speed"]
        self.gas[b] = f["gas"]
        self.brake[b] = f["brake"]
        self.gear[b] = f["gear"]
        self.steer[b] = f["steer"]
        if f["abs_active"]:
            self.abs_bins.add(b)
        if f["tc_active"]:
            self.tc_bins.add(b)

    def arrays(self):
        return {"t": self.t, "speed": self.speed, "gas": self.gas, "brake": self.brake, "steer": self.steer}

    def coverage(self):
        return sum(v is not None for v in self.speed) / self.n

    def finalize(self, ms):
        self.ms = ms
        self.t[0] = 0 if self.t[0] is None else self.t[0]
        if self.t[-1] is None and ms:
            self.t[-1] = ms
        arrays = {}
        for k in ("t", "speed", "gas", "brake", "gear", "steer"):
            a = _fill(getattr(self, k))
            if a is None:
                return None
            arrays[k] = a
        return arrays

    def count(self, kind, b0, b1):
        return sum(1 for b, k in self.events if k == kind and b0 <= b <= b1)


class Corner:
    def __init__(self, cid, start, apex, end, brake_bin, ref, bin_m):
        self.id = cid
        self.start, self.apex, self.end = start, apex, end
        self.brake_bin = brake_bin
        self.ref_min = min(ref["speed"][start:end + 1])
        self.ref_exit = ref["speed"][end]
        self.ref_gear = int(round(ref["gear"][apex]))
        self.ref_dt = ref["t"][end] - ref["t"][start]
        self.ref_brake_kmh = ref["speed"][brake_bin] if brake_bin is not None else None
        self.kind = corner_class(self.ref_min)
        self.ref_ph = pass_phases(ref, self, bin_m)
        self.ref_brake = brake_stats(ref, self, bin_m) if brake_bin is not None else None
        self.history = deque(maxlen=6)
        self.last = None
        self.last_brake = None
        self.best_dt = None

    def contains(self, b):
        return self.start <= b <= self.end


def detect_corners(ref, bin_m):
    sp = ref["speed"]
    n = len(sp)
    sm = [sum(sp[(i + k) % n] for k in range(-3, 4)) / 7 for i in range(n)]
    w = max(2, int(50 / bin_m))
    back = max(5, int(400 / bin_m))
    mins = []
    for i in range(n):
        v = sm[i]
        if any(sm[(i + k) % n] < v for k in range(-w, w + 1) if k):
            continue
        pre = max(sm[(i - k) % n] for k in range(1, back))
        post = max(sm[(i + k) % n] for k in range(1, back))
        if pre - v >= 10 and post - v >= 8:
            mins.append(i)
    merged = []
    for i in mins:
        if merged and (i - merged[-1]) * bin_m < 80:
            if sm[i] < sm[merged[-1]]:
                merged[-1] = i
        else:
            merged.append(i)
    corners = []
    limit_back = int(300 / bin_m)
    for apex in merged:
        lo = max(0, apex - limit_back)
        seg = sm[lo:apex + 1]
        peak = lo + seg.index(max(seg))
        brake_bin = None
        for k in range(peak, apex + 1):
            if ref["brake"][k] > 0.1:
                brake_bin = k
                break
        start = max(0, (brake_bin if brake_bin is not None else peak) - 2)
        end = apex
        lim = min(n - 1, apex + int(350 / bin_m))
        while end < lim and ref["gas"][end] < 0.9:
            end += 1
        end = min(n - 1, end + int(50 / bin_m))
        if corners and start <= corners[-1].end:
            start = corners[-1].end + 1
        if end - start < 3:
            continue
        corners.append(Corner(len(corners) + 1, start, apex, end, brake_bin, ref, bin_m))
    return corners


class Engine:
    def __init__(self, tracker=None):
        self.lock = threading.RLock()
        self.tracker = tracker
        self.connected = False
        self.last_frame = None
        self.msg_id = 0
        self.messages = deque(maxlen=40)
        self.spot_msgs = deque(maxlen=5)
        self.reset_session(None)

    # ------------------------------------------------------------ sessão
    def reset_session(self, f):
        self.sim = f["sim"] if f else ""
        self.car = f["car"] if f else ""
        self.track = f["track"] if f else ""
        tl = f["track_len"] if f and f["track_len"] > 100 else 4000.0
        self.track_len = tl
        self.n = int(min(5000, max(200, tl / 5)))
        self.bin_m = tl / self.n
        self.lap = None
        self.lap_no = None
        self.laps = []
        self.ref = None
        self.ref_provisional = True
        self.ref_version = 0
        self.corners = []
        self.prev_bin = None
        self.wheel_r = [None] * 4
        self.lock_frames = [0] * 4
        self.overlap_t = 0.0
        self.bal_s = 0.0
        self.slide_t = {"under": 0.0, "over": 0.0}
        self.slide_on = {"under": False, "over": False}
        self.last_t = None
        self.was_valid = True
        self.bal = {}
        self.tyre = {k: [None] * 4 for k in ("p", "core", "i", "m", "o")}
        self.brake_hot = [None] * 4
        self.warm_s = 0.0
        self.fuel_laps = deque(maxlen=8)
        self.cooldown = {}
        self.setup_tips = []
        self.setup_at = 0
        self.fuel_save = False
        self.pit_loss = None
        self.battle_prev = {}
        self.strategy_msg_lap = None
        self.spot = ""
        self.spot_clear_t = 0.0
        self.map_saved_version = -1
        self.session_started = time.strftime("%Y%m%d_%H%M%S")
        if f:
            self._load_ref()
            self._load_pit_loss()
            self._load_map()
            self.say("info", "sessao", f"Sessão: {self.car} em {self.track} ({self.sim.upper()})", speak=False)

    def key(self):
        return f"{slug(self.sim)}__{slug(self.car)}__{slug(self.track)}"

    def _load_ref(self):
        path = os.path.join(DATA_DIR, "referencias", self.key() + ".json")
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            if len(d["speed"]) == self.n:
                self._set_ref(d, provisional=False, announce=False)
                self.say("info", "ref", f"Referência carregada: {fmt_ms(d['ms'])} (melhor volta salva)", speak=False)
        except (OSError, ValueError, KeyError):
            pass

    def _save_ref(self):
        os.makedirs(os.path.join(DATA_DIR, "referencias"), exist_ok=True)
        with open(os.path.join(DATA_DIR, "referencias", self.key() + ".json"), "w", encoding="utf-8") as fh:
            json.dump(self.ref, fh)

    def _pit_path(self):
        return os.path.join(DATA_DIR, "pit", f"{slug(self.sim)}__{slug(self.track)}.json")

    def _load_pit_loss(self):
        try:
            with open(self._pit_path(), encoding="utf-8") as fh:
                self.pit_loss = float(json.load(fh)["loss_s"])
        except (OSError, ValueError, KeyError):
            self.pit_loss = None

    def _map_path(self):
        return os.path.join(DATA_DIR, "mapas", f"{slug(self.sim)}__{slug(self.track)}.json")

    def _load_map(self):
        if not self.tracker:
            return
        try:
            with open(self._map_path(), encoding="utf-8") as fh:
                self.tracker.reset_map(json.load(fh)["pts"])
        except (OSError, ValueError, KeyError):
            self.tracker.reset_map()
        self.map_saved_version = self.tracker.map_version

    def _save_map(self):
        tr = self.tracker
        if not tr or tr.map_version == self.map_saved_version or tr.map_coverage() < 0.9:
            return
        os.makedirs(os.path.dirname(self._map_path()), exist_ok=True)
        try:
            with open(self._map_path(), "w", encoding="utf-8") as fh:
                json.dump({"pts": [[round(p[0], 1), round(p[1], 1)] if p else None for p in tr.map_bins]}, fh)
            self.map_saved_version = tr.map_version
        except OSError:
            pass

    def _spotter(self, f):
        """Spotter: avisa (voz + barras laterais) carro à esquerda/direita e quando fica livre."""
        tr = self.tracker
        if not tr or self.sim == "ac" or f["in_pit"] or f["speed"] < 30:
            self.spot = ""
            return
        res = tr.spotter()
        if res is None:
            return
        left, right = res
        state = ("L" if left else "") + ("R" if right else "")
        now = f["t"]
        if state:
            self.spot_clear_t = 0.0
            if state != self.spot:
                text = {"L": "Carro à esquerda", "R": "Carro à direita", "LR": "No meio — carros dos dois lados"}[state]
                self.say("warn", "spotter", text)
                self.spot = state
        elif self.spot:
            if not self.spot_clear_t:
                self.spot_clear_t = now
            elif now - self.spot_clear_t >= 0.5:
                self.say("good", "spotter", "Livre")
                self.spot = ""
                self.spot_clear_t = 0.0

    def map_payload(self):
        tr = self.tracker
        if not tr:
            return {"version": 0, "pts": []}
        with tr.lock:
            return {"version": tr.map_version, "pts": [[round(p[0], 1), round(p[1], 1)] if p else None for p in tr.map_bins]}

    def _save_session(self):
        os.makedirs(os.path.join(DATA_DIR, "sessoes"), exist_ok=True)
        path = os.path.join(DATA_DIR, "sessoes", f"{self.session_started}__{self.key()}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"sim": self.sim, "car": self.car, "track": self.track, "laps": self.laps,
                       "setup": self.setup_tips}, fh, ensure_ascii=False, indent=1)

    def _set_ref(self, arrays, provisional, announce=True):
        self.ref = arrays
        self.ref_provisional = provisional
        self.ref_version += 1
        self.corners = detect_corners(arrays, self.bin_m)
        if announce and not provisional:
            self.say("good", "ref", f"Nova melhor volta {fmt_ms(arrays['ms'])} — virou a referência", speak=True)

    # ------------------------------------------------------------ mensagens
    def say(self, level, cat, text, speak=True, cooldown_key=None, cooldown=4.0):
        now = time.time()
        if cooldown_key:
            if now - self.cooldown.get(cooldown_key, 0) < cooldown:
                return
            self.cooldown[cooldown_key] = now
        self.msg_id += 1
        msg = {"id": self.msg_id, "t": now, "level": level, "cat": cat, "text": text, "speak": speak}
        # spotter fala muito: fila própria para não empurrar as dicas do coach para fora da tela
        (self.spot_msgs if cat == "spotter" else self.messages).append(msg)

    def where(self, b):
        for c in self.corners:
            if c.contains(b):
                return f"T{c.id}"
        # logo depois do fim da zona da curva ainda é "saída" dela
        for c in self.corners:
            if 0 < b - c.end <= int(150 / self.bin_m):
                return f"T{c.id}"
        return "na reta"

    # ------------------------------------------------------------ loop principal
    def process(self, f):
        with self.lock:
            if f is None:
                self.connected = False
                return
            self.connected = True
            if (f["sim"], f["car"], f["track"]) != (self.sim, self.car, self.track):
                self.reset_session(f)
            self.last_frame = f
            if f["status"] != 2:  # só analisa ao vivo (não replay/pause)
                self.last_t = None
                return
            dt = 0.0 if self.last_t is None else max(0.0, min(0.2, f["t"] - self.last_t))
            self.last_t = f["t"]
            b = min(self.n - 1, max(0, int(f["pos"] * self.n)))

            self._lap_boundary(f, b)
            if self.lap and f["in_pit"]:
                self.lap.pit = True
            if self.lap and not f["in_pit"]:
                if not (b < self.n * 0.05 and f["cur_ms"] > 10000) and not (b > self.n * 0.95 and f["cur_ms"] < 5000):
                    self.lap.record(b, f)
                if self.was_valid and not f["valid"]:
                    self.say("bad", "limites", f"Volta invalidada {self.where(b)} — limites de pista", cooldown_key="inv")
                self.was_valid = f["valid"]
                if not f["valid"]:
                    self.lap.valid = False
                if f["cur_ms"] and self.lap.sector_marks.get(f["sector"]) is None:
                    self.lap.sector_marks[f["sector"]] = f["cur_ms"]
            if f["speed"] > 30 and not f["in_pit"]:
                self.warm_s += dt
                self._live_events(f, b, dt)
                self._live_slide(f, b, dt)
                self._balance(f)
                self._tyres(f)
            self._corner_exit(b)
            self.prev_bin = b
            self._opponent_events(f)
            self._spotter(f)
            if time.time() - self.setup_at > 2.0:
                self.setup_at = time.time()
                self.setup_tips = self._setup_advice()

    def _green_pace(self):
        green = [l["ms"] for l in self.laps[-6:] if l["complete"] and l["valid"] and not l.get("pit")]
        if not green:
            green = [l["ms"] for l in self.laps[-6:] if l["complete"] and not l.get("pit")]
        return statistics.median(green) / 1000 if green else None

    def _lap_boundary(self, f, b):
        if self.lap_no is None or f["lap"] < self.lap_no:
            self._start_lap(f, b)
            return
        if f["lap"] == self.lap_no:
            return
        lap = self.lap
        ms = f["last_ms"]
        arrays = lap.finalize(ms) if lap else None
        complete = lap is not None and lap.clean and lap.coverage() > 0.85 and arrays is not None and ms > 0
        if lap is not None and ms > 0:
            fuel_used = (lap.fuel_start or 0) - f["fuel"]
            if complete and not lap.pit and 0 < fuel_used < 50:
                self.fuel_laps.append(fuel_used)
            best = min((l["ms"] for l in self.laps if l["valid"] and l["complete"]), default=0)
            secs = sorted(lap.sector_marks.items())
            sectors = []
            for i, (_, start_ms) in enumerate(secs):
                end_ms = secs[i + 1][1] if i + 1 < len(secs) else ms
                sectors.append(end_ms - start_ms)
            rec = {"n": lap.number + 1, "ms": ms, "time": fmt_ms(ms), "valid": lap.valid, "complete": complete,
                   "pit": lap.pit, "delta_best": (ms - best) if best else 0, "fuel": round(fuel_used, 2),
                   "locks": sum(1 for _, k in lap.events if k.startswith("lock")),
                   "spins": sum(1 for _, k in lap.events if k == "spin"),
                   "long_coasts": sum(1 for _, k in lap.events if k == "long_coast"),
                   "early_gas": sum(1 for _, k in lap.events if k == "early_gas"),
                   "entry_lifts": sum(1 for _, k in lap.events if k == "entry_lift"),
                   "coast_s": round(lap.coast_s, 1), "sectors": sectors if len(sectors) > 1 else []}
            pace_before = self._green_pace()
            self.laps.append(rec)
            self._measure_pit_loss(pace_before)
            if complete and not lap.pit:
                arrays["ms"] = ms
                ref_ms = self.ref["ms"] if self.ref else None
                best_before = min([x for x in (best, ref_ms if self.ref and not self.ref_provisional else 0) if x], default=0)
                prev = next((l["ms"] for l in reversed(self.laps[:-1]) if l["complete"] and not l.get("pit")), None)
                self._lap_summary(rec, lap, ms, prev, best_before)
                if lap.valid and (self.ref is None or self.ref_provisional or ms < ref_ms):
                    self._set_ref(arrays, provisional=False, announce=False)
                    self._save_ref()
                elif self.ref is None:
                    self._set_ref(arrays, provisional=True)
                    self.say("info", "ref", "Curvas mapeadas a partir desta volta (inválida). Faça uma volta limpa para virar referência.", speak=False)
            try:
                self._save_session()
            except OSError:
                pass
            self._race_lap_messages(f)
            self._save_map()
        self._start_lap(f, b)

    def _lap_summary(self, rec, lap, ms, prev, best):
        """Resumo falado no fim da volta: ganho vs anterior e vs melhor, onde ganhou/perdeu, potencial."""
        parts = [f"Volta {rec['n']}: {fmt_ms(ms)}"]
        level = "info"
        if prev:
            d = (ms - prev) / 1000
            if abs(d) >= 0.01:
                parts.append(f"{abs(d):.2f}s {'mais rápida' if d < 0 else 'mais lenta'} que a anterior")
                level = "good" if d < 0 else "warn"
        if not lap.valid:
            parts.append("inválida")
            level = "warn"
        elif best:
            db = (ms - best) / 1000
            if db < 0:
                parts.append(f"NOVA MELHOR, {-db:.2f}s mais rápida que a melhor anterior")
                level = "good"
            else:
                parts.append(f"+{db:.2f}s da melhor")
        else:
            parts.append("primeira volta de referência")
        if lap.corner_dt:
            cid_g, g = min(lap.corner_dt.items(), key=lambda x: x[1])
            cid_l, l = max(lap.corner_dt.items(), key=lambda x: x[1])
            if g < -0.03:
                parts.append(f"ganhou mais na T{cid_g} ({g:+.2f}s)")
            if l > 0.05:
                parts.append(f"perdeu mais na T{cid_l} (+{l:.2f}s)")
        ideal_gain = self._ideal_gain()
        if ideal_gain > 0.1:
            parts.append(f"potencial: {ideal_gain:.2f}s juntando suas melhores curvas")
        self.say(level, "volta", " — ".join(parts))

    def _ideal_gain(self):
        """Quanto você ganharia sobre a referência juntando sua melhor passagem em cada curva."""
        return -sum(min(0.0, c.best_dt) for c in self.corners if c.best_dt is not None)

    def _start_lap(self, f, b):
        clean = f["cur_ms"] < 4000 and (b < self.n * 0.05 or b > self.n * 0.97)
        self.lap = Lap(f["lap"], self.n, clean, f["fuel"])
        self.lap_no = f["lap"]
        self.was_valid = True

    def _measure_pit_loss(self, pace):
        """Volta(s) com pit seguidas de uma volta normal: perda = soma - n x ritmo."""
        if len(self.laps) < 2 or self.laps[-1].get("pit") or not pace:
            return
        pit_laps = []
        for l in reversed(self.laps[:-1]):
            if not l.get("pit"):
                break
            pit_laps.append(l)
        if not pit_laps or len(pit_laps) > 2:
            return
        loss = sum(l["ms"] for l in pit_laps) / 1000 - len(pit_laps) * pace
        if 5 < loss < 180:
            self.pit_loss = loss
            os.makedirs(os.path.dirname(self._pit_path()), exist_ok=True)
            try:
                with open(self._pit_path(), "w", encoding="utf-8") as fh:
                    json.dump({"loss_s": loss}, fh)
            except OSError:
                pass
            self.say("info", "pit", f"Perda medida no pit: {loss:.1f}s (entrada + parada + saída). Uso esse valor nas projeções.")

    # ------------------------------------------------------------ eventos ao vivo
    def _live_events(self, f, b, dt):
        lap = self.lap
        v = f["speed"] / 3.6
        w = f["wheel_w"]
        straight = abs(f["steer"]) < 0.06 and f["brake"] < 0.02 and f["gas"] < 0.6 and abs(f["acc_lat"]) < 0.3
        for k in range(4):
            if abs(w[k]) > 1:
                r = v / abs(w[k])
                if straight and 0.15 < r < 0.6:
                    self.wheel_r[k] = r if self.wheel_r[k] is None else self.wheel_r[k] * 0.98 + r * 0.02
        if v > 8 and all(self.wheel_r):
            ratio = [abs(w[k]) * self.wheel_r[k] / v for k in range(4)]
            if f["brake"] > 0.2:
                for k in range(4):
                    self.lock_frames[k] = self.lock_frames[k] + 1 if ratio[k] < CFG["lock_ratio"] else 0
                    if self.lock_frames[k] == 3:
                        where = self.where(b)
                        side = ["dianteira esquerda", "dianteira direita", "traseira esquerda", "traseira direita"][k]
                        kind = "lock_f" if k < 2 else "lock_r"
                        if lap:
                            lap.events.append((b, kind))
                        tip = "alivie o pico do pedal" if k < 2 else "bias mais para frente / menos freio-motor"
                        self.say("bad", "freio", f"Travou {side} {where} — {tip}", cooldown_key=f"lock{k}{where}")
            else:
                self.lock_frames = [0] * 4
            if f["gas"] > 0.3 and (ratio[2] + ratio[3]) / 2 > CFG["spin_ratio"] and f["speed"] > 25:
                where = self.where(b)
                if self.cooldown.get(f"spin{where}", 0) < time.time() - 4 and lap:
                    lap.events.append((b, "spin"))
                self.say("warn", "tracao", f"{where}: patinando na saída, traseira pode escapar — acelere mais progressivo", cooldown_key=f"spin{where}")
        # freio e acelerador juntos (rolagem é analisada por curva, nas fases — ver _evaluate_corner)
        if f["gas"] > 0.2 and f["brake"] > 0.2:
            self.overlap_t += dt
            if self.overlap_t > 0.3:
                self.say("warn", "tecnica", f"Freio e acelerador juntos {self.where(b)}", cooldown_key="overlap", cooldown=10)
                if lap:
                    lap.events.append((b, "overlap"))
                self.overlap_t = -99
        else:
            self.overlap_t = 0.0

    def _live_slide(self, f, b, dt):
        """
        Saindo de frente / de traseira, na hora:
        - traseira: o carro gira (yaw) mais rápido do que a curva pede (yaw > a_lat / v),
          ou os pneus traseiros escorregam bem mais que os dianteiros;
        - frente: os dianteiros escorregam bem mais que os traseiros com o carro carregado lateralmente.
        """
        if f["speed"] < 50:
            self.slide_t = {"under": 0.0, "over": 0.0}
            self.slide_on = {"under": False, "over": False}
            return
        sa = [abs(x) for x in f["slip_angle"]]
        fr, rr = (sa[0] + sa[1]) / 2, (sa[2] + sa[3]) / 2
        inst = (fr - rr) / (fr + rr) if fr + rr > 1e-4 else 0.0
        self.bal_s += (inst - self.bal_s) * min(1.0, dt / 0.15)
        v = f["speed"] / 3.6
        alat = abs(f["acc_lat"]) * 9.81
        yaw = abs(f["yaw"])
        yaw_excess = yaw > 0.15 and yaw > (alat / v) * 1.3 + 0.05
        loaded = abs(f["acc_lat"]) > 0.6
        state = {"over": (loaded or yaw > 0.25) and (self.bal_s < -0.2 or yaw_excess),
                 "under": loaded and self.bal_s > 0.25 and not yaw_excess}
        need = {"over": 0.15, "under": 0.3}
        phase = "entrada" if f["brake"] > 0.05 else ("saida" if f["gas"] > 0.4 else "meio")
        for kind in ("over", "under"):
            if state[kind]:
                self.slide_t[kind] += dt
                if self.slide_t[kind] >= need[kind] and not self.slide_on[kind]:
                    self.slide_on[kind] = True
                    where = self.where(b)
                    spun = self.lap and any(k == "spin" and 0 <= b - eb <= int(60 / self.bin_m) for eb, k in self.lap.events[-5:])
                    if self.lap:
                        self.lap.events.append((b, f"{kind}_{phase}"))
                    if spun and kind == "over" and phase == "saida":
                        continue  # já avisado como patinagem
                    self.say("bad" if kind == "over" else "warn", "equilibrio",
                             f"{where[0].upper() + where[1:]}: {SLIDE_TIPS[(kind, phase)]}",
                             cooldown_key=f"slide{kind}{where}{self.lap_no}", cooldown=60)
            else:
                self.slide_t[kind] = 0.0
                self.slide_on[kind] = False

    def _corner_exit(self, b):
        if self.prev_bin is None or not self.ref or not self.lap or b < self.prev_bin:
            return
        for c in self.corners:
            if self.prev_bin < c.apex <= b:
                self._evaluate_braking(c)
            if self.prev_bin < c.end <= b:
                self._evaluate_corner(c)

    def _evaluate_braking(self, c):
        """No ápice: ponto de frenagem, pico de pressão e rapidez para chegar no pico vs. referência."""
        if c.ref_brake is None:
            return
        cur = brake_stats(self.lap.arrays(), c, self.bin_m)
        if cur is None:
            return
        ref = c.ref_brake
        d_m = (ref["bp"] - cur["bp"]) * self.bin_m  # >0 = freou antes
        parts, lvl = [], "warn"
        if d_m > 10 and not self.fuel_save:
            parts.append(f"freou {d_m:.0f} m antes da referência — dá para frear mais tarde")
        elif d_m < -10:
            parts.append(f"freou {-d_m:.0f} m depois da referência")
            lvl = "info"
        if ref["peak"] > 0.4 and cur["peak"] < ref["peak"] - 0.12:
            parts.append(f"pico de freio {cur['peak'] * 100:.0f}% (ref {ref['peak'] * 100:.0f}%) — pise forte logo no início e vá soltando")
            lvl = "warn"
        if cur["rise_ms"] is not None and ref["rise_ms"] is not None and cur["rise_ms"] - ref["rise_ms"] > 150:
            parts.append(f"levou {(cur['rise_ms'] - ref['rise_ms']) / 1000:.1f}s a mais para chegar no pico — ataque o pedal mais rápido")
            lvl = "warn"
        prev = c.last_brake
        c.last_brake = {"d_m": round(d_m), "peak": round(cur["peak"] * 100), "ref_peak": round(ref["peak"] * 100)}
        if parts:
            # mesmo problema na mesma curva: repete no máximo a cada ~2 voltas
            self.say(lvl, "frenagem", f"T{c.id} frenagem: " + "; ".join(parts[:2]),
                     cooldown_key=f"brk{c.id}{parts[0][:12]}", cooldown=150)
        elif prev and abs(prev["d_m"]) > 10 and abs(d_m) <= 5:
            self.say("good", "frenagem", f"T{c.id}: frenagem no ponto ✓", speak=False)

    def _evaluate_corner(self, c):
        lap, ref = self.lap, self.ref
        if any(v is None for v in (lap.t[c.start], lap.t[c.end])):
            return
        sp = [v for v in lap.speed[c.start:c.end + 1] if v is not None]
        if len(sp) < (c.end - c.start) * 0.6:
            return
        dt = ((lap.t[c.end] - lap.t[c.start]) - c.ref_dt) / 1000.0
        mn = min(sp)
        d_min = mn - c.ref_min
        ex = lap.speed[c.end] or sp[-1]
        d_exit = ex - c.ref_exit
        ph = pass_phases(lap.arrays(), c, self.bin_m)
        rph = c.ref_ph
        lo = max(0, c.start - int(150 / self.bin_m))
        bp = next((k for k in range(lo, c.apex + 1) if lap.brake[k] is not None and lap.brake[k] > 0.1), None)
        ref_bp = c.brake_bin
        reasons = []
        if lap.count("lock_f", c.start, c.end) or lap.count("lock_r", c.start, c.end):
            reasons.append((3, "travou roda na frenagem"))
        if bp is not None and ref_bp is not None:
            d_bp = (ref_bp - bp) * self.bin_m  # >0 = freou antes da ref
            # frenagem cedo já foi comentada no ápice (_evaluate_braking); aqui só o efeito na saída
            if d_bp < -10 and (d_min < -3 or d_exit < -3):
                reasons.append((2.5, f"freou {-d_bp:.0f} m mais tarde e perdeu a saída"))
        # --- fases: tirar o pé antes de frear (rolagem acelerador -> freio)
        ref_lift = rph["entry_lift_s"] if rph else 0
        if ph["entry_lift_s"] > CFG["entry_lift_s"] and ph["entry_lift_s"] > ref_lift + 0.2 and not self.fuel_save:
            reasons.append((2.4, f"tirou o pé {ph['entry_lift_s']:.1f}s antes de frear — acelere até o ponto de frenagem"))
            lap.events.append((c.start, "entry_lift"))
        # --- fases: acelerou cedo demais e teve que aliviar (erro mais comum segundo os coaches)
        if ph["hesitation"] and (ph["early"] or not (rph and rph["hesitation"])):
            reasons.append((2.3, "acelerou antes de poder abrir o volante e teve que aliviar — espere o ponto de abrir a direção"))
            lap.events.append((c.apex, "early_gas"))
        # --- fases: rolagem freio -> acelerador. Pausa curta é normal; longa (vs. tipo de curva e vs. sua melhor volta) é perda
        car_len = ph["coast_m"] / CFG["car_len_m"]
        ok_len = CFG["coast_ok_len"][c.kind]
        ref_coast = rph["coast_s"] if rph else 0.0
        if car_len > ok_len and ph["coast_s"] > ref_coast + CFG["coast_vs_ref_s"]:
            reasons.append((2.2, f"rolou {ph['coast_s']:.1f}s (~{car_len:.0f} carros) entre soltar o freio e acelerar "
                                 f"({ref_coast:.1f}s na sua melhor) — solte o freio mais perto do ápice ou entre com acelerador parcial"))
            lap.events.append((c.apex, "long_coast"))
        lap.coast_s += ph["coast_s"]
        if d_min < -3:
            reasons.append((2, f"mínima {-d_min:.0f} km/h abaixo — solte o freio antes e leve velocidade ao ápice"))
        elif d_min > 3 and d_exit < -3:
            reasons.append((2, f"entrou rápido demais (+{d_min:.0f} km/h) e comprometeu a saída"))
        if d_exit < -4:
            reasons.append((1.5, f"saída {-d_exit:.0f} km/h mais lenta — retome o acelerador mais cedo, progressivo"))
        for kind, label in (("over", "traseira escapou"), ("under", "saiu de frente")):
            ph_hit = [p for p in ("entrada", "meio", "saida") if lap.count(f"{kind}_{p}", c.start, c.end)]
            if ph_hit:
                reasons.append((2.6 if kind == "over" else 2.0, f"{label} ({', '.join(ph_hit)})"))
        if lap.count("spin", c.start, c.end):
            why = "patinou na saída" + (" — acelerador de 0 a 100% muito rápido" if ph["abrupt"] else "")
            reasons.append((1.5, why))
        abs_share = len([k for k in lap.abs_bins if c.start <= k <= c.apex]) / max(1, c.apex - c.start)
        if abs_share > 0.5:
            reasons.append((1, "ABS atuando quase a frenagem toda"))
        g = lap.gear[c.apex]
        if g is not None and c.ref_gear and g != c.ref_gear and dt > 0.05:
            reasons.append((1, f"referência usou {c.ref_gear}ª, você {g}ª"))
        reasons.sort(key=lambda r: -r[0])
        top = [r[1] for r in reasons[:2]]
        c.history.append(dt)
        lap.corner_dt[c.id] = dt
        if lap.valid:
            c.best_dt = dt if c.best_dt is None else min(c.best_dt, dt)
        c.last = {"dt": round(dt, 3), "min": round(mn), "exit": round(ex), "reasons": top,
                  "coast_s": round(ph["coast_s"], 2), "ref_coast_s": round(ref_coast, 2)}
        if dt <= -0.03:
            self.say("good", "curva", f"T{c.id}: ganhou {-dt:.2f}s ✓", speak=False)
        elif dt >= 0.05:
            lvl = "bad" if dt > 0.2 else "warn"
            why = top[0] if top else "linha/velocidade diferente da referência"
            self.say(lvl, "curva", f"T{c.id}: +{dt:.2f}s — {why}")

    # ------------------------------------------------------------ equilíbrio e pneus
    def _balance(self, f):
        if abs(f["acc_lat"]) < 0.5 or f["speed"] < 40:
            return
        sa = [abs(x) for x in f["slip_angle"]]
        front, rear = (sa[0] + sa[1]) / 2, (sa[2] + sa[3]) / 2
        if front + rear < 1e-4:
            return
        val = (front - rear) / (front + rear)
        phase = "entrada" if f["brake"] > 0.05 else ("saida" if f["gas"] > 0.4 else "meio")
        spd = "lenta" if f["speed"] < 130 else "rapida"
        k = f"{phase}_{spd}"
        m, cnt = self.bal.get(k, (0.0, 0))
        a = max(0.0008, 1.0 / (cnt + 1))
        self.bal[k] = (m + (val - m) * a, cnt + 1)

    def _tyres(self, f):
        a = 0.01
        for key, arr in (("p", f["pressure"]), ("core", f["core"]), ("i", f["t_in"]), ("m", f["t_mid"]), ("o", f["t_out"])):
            cur = self.tyre[key]
            for k in range(4):
                if arr[k] > 0:
                    cur[k] = arr[k] if cur[k] is None else cur[k] + (arr[k] - cur[k]) * a
        if f["brake"] > 0.3:
            for k in range(4):
                bt = f["brake_temp"][k]
                if bt > 0:
                    cur = self.brake_hot[k]
                    self.brake_hot[k] = bt if cur is None else cur + (bt - cur) * 0.02

    # ------------------------------------------------------------ corrida: estratégia
    def _strategy(self, f):
        pace = self._green_pace()
        per = sum(self.fuel_laps) / len(self.fuel_laps) if self.fuel_laps else None
        left_s = f["session_left_ms"] / 1000 if f["session_left_ms"] else 0
        frac = f["pos"]
        laps_to_go = None
        if f["timed"]:
            if pace and left_s > 0:
                # termina quando cruzar a linha depois do tempo acabar
                laps_to_go = math.ceil(frac + left_s / pace) - frac + (1 if f["extra_lap"] else 0)
        elif f["total_laps"] > 0:
            laps_to_go = max(0.0, f["total_laps"] - f["lap"] - frac)
        out = {"pace": round(pace, 3) if pace else None, "time_left": fmt_clock(left_s) if left_s else None,
               "laps_to_go": round(laps_to_go, 1) if laps_to_go is not None else None,
               "pit_loss": round(self.pit_loss or CFG["pit_loss_s"], 1), "pit_loss_measured": self.pit_loss is not None,
               "pit_missing": f["pit_missing"], "mfd_fuel": f["mfd_fuel"], "fuel_now": round(f["fuel"], 1),
               "per_lap": round(per, 2) if per else None, "fuel_save": self.fuel_save}
        if not per:
            out["plan"] = "Complete 1–2 voltas limpas para calcular o consumo."
            return out
        out["runs_out_in"] = round(f["fuel"] / per, 1)
        if laps_to_go is None:
            out["plan"] = f"Tanque atual dá ~{f['fuel'] / per:.1f} voltas."
            return out
        need = laps_to_go * per
        margin = CFG["fuel_margin_laps"] * per
        bal = f["fuel"] - need
        add = max(0.0, need + margin - f["fuel"])
        out.update(need=round(need, 1), balance=round(bal, 1), add=math.ceil(add), target=round(f["fuel"] / laps_to_go, 2) if laps_to_go else None)
        mode = "ok"
        if bal >= margin:
            plan = f"Dá até o fim: sobra {bal:.1f} L (~{bal / per:.1f} voltas)."
        elif bal >= 0:
            plan = f"Dá até o fim com margem curta ({bal:.1f} L). Evite voltas de ataque desnecessárias."
        else:
            deficit = -bal
            if f["pit_missing"]:
                plan = f"Pit obrigatório pendente: na parada coloque ~{math.ceil(add)} L (inclui {CFG['fuel_margin_laps']:.0f} volta de margem)."
                mode = "pit"
            elif deficit / max(need, 1e-6) <= CFG["fuel_save_max_pct"]:
                plan = (f"Faltam {deficit:.1f} L. Sem parar: economize {deficit / laps_to_go:.2f} L/volta "
                        f"(meta {f['fuel'] / laps_to_go:.2f} L/volta) com lift & coast. Ou pare e coloque ~{math.ceil(add)} L.")
                mode = "save"
            else:
                plan = (f"Pit necessário: coloque ~{math.ceil(add)} L. O tanque acaba em ~{f['fuel'] / per:.1f} voltas "
                        f"(volta {f['lap'] + 1 + int(f['fuel'] / per)}).")
                mode = "pit"
        out["plan"] = plan
        out["mode"] = mode
        if mode == "save":
            # retas mais rápidas = maior ganho de combustível com lift & coast
            cand = sorted([c for c in self.corners if c.ref_brake_kmh], key=lambda c: -c.ref_brake_kmh)[:3]
            out["lift_corners"] = [f"T{c.id}" for c in sorted(cand, key=lambda c: c.id)]
        return out

    def _race_lap_messages(self, f):
        """Chamado a cada volta completada: combustível, duelos."""
        is_race = f["session"] == 2 or (self.tracker and self.tracker.session.get("is_race"))
        st = self._strategy(f)
        new_save = st.get("mode") == "save"
        if new_save and not self.fuel_save:
            corners = ", ".join(st.get("lift_corners", []))
            self.say("warn", "estrategia", f"Modo economia: lift & coast antes de {corners or 'as frenagens do fim das retas'} — "
                     f"tire o pé um pouco antes da placa, role e freie mantendo a mesma velocidade de ápice. Meta {st.get('target')} L/volta.")
        elif self.fuel_save and not new_save:
            self.say("good", "estrategia", "Combustível OK de novo — pode voltar a acelerar até a placa.")
        self.fuel_save = new_save
        if not is_race:
            return
        if st.get("mode") == "pit" and self.strategy_msg_lap != f["lap"]:
            self.strategy_msg_lap = f["lap"]
            self.say("warn", "estrategia", st["plan"])
        elif new_save and self.fuel_laps:
            last = self.laps[-1]["fuel"] if self.laps else 0
            tgt = st.get("target") or 0
            if last and tgt:
                ok = last <= tgt
                self.say("good" if ok else "warn", "estrategia",
                         f"Consumo da volta: {last:.2f} L (meta {tgt:.2f}) — {'dentro da meta' if ok else 'tire o pé um pouco antes'}")
        self._battle_messages()

    # ------------------------------------------------------------ corrida: oponentes
    def _opponent_events(self, f):
        tr = self.tracker
        if not tr:
            return
        me = tr.cars.get(tr.player)
        for kind, idx, t in tr.pop_events():
            if kind != "pit_in" or me is None or idx == tr.player:
                continue
            c = tr.cars.get(idx)
            if c and me.position and c.position and abs(c.position - me.position) <= 3:
                rel = "à frente" if c.position < me.position else "atrás"
                tip = ("se você ainda vai parar, parar na próxima volta responde ao undercut; "
                       "se não, empurre agora com pista livre (overcut)")
                self.say("info", "rivais", f"#{c.num} {c.driver} (P{c.position}, {rel}) entrou no box — {tip}",
                         cooldown_key=f"pit{idx}", cooldown=60)

    def _battle_messages(self):
        tr = self.tracker
        if not tr:
            return
        me = tr.cars.get(tr.player)
        if not me or not me.position:
            return
        by_pos = {c.position: c for c in tr.standings()}
        for rel, pos in (("ahead", me.position - 1), ("behind", me.position + 1)):
            c = by_pos.get(pos)
            if not c or c.in_pit:
                continue
            g = tr.gap(me, c)
            if g is None:
                continue
            prev = self.battle_prev.get(c.idx)
            self.battle_prev[c.idx] = g
            if prev is None or c.pits != self.battle_prev.get(("pits", c.idx), c.pits):
                self.battle_prev[("pits", c.idx)] = c.pits
                continue
            if abs(g - prev) > 2.5:  # ciclo de pit / dado ruim: não é ritmo
                continue
            d = g - prev  # variação por volta (positivo = ele abriu à frente / chegou mais perto atrás... ver abaixo)
            if rel == "ahead" and 0 < g < 8:
                if d < -0.05:
                    laps = g / -d
                    self.say("info", "rivais", f"Ataque: #{c.num} {c.driver} (P{c.position}) a {g:.1f}s — você tira {-d:.2f}s/volta"
                             + (f", alcança em ~{laps:.0f} voltas" if laps < 30 else ""))
                elif d > 0.1:
                    self.say("info", "rivais", f"#{c.num} à frente abre {d:.2f}s/volta ({g:.1f}s)", speak=False)
            elif rel == "behind" and -6 < g < 0:
                if d > 0.05:
                    self.say("warn", "rivais", f"Defesa: #{c.num} {c.driver} a {-g:.1f}s atrás, tirando {d:.2f}s/volta — capriche nas saídas de curva")
                elif d < -0.1:
                    self.say("good", "rivais", f"Você abre {-d:.2f}s/volta do #{c.num} atrás ({-g:.1f}s)", speak=False)

    def _race_view(self):
        tr = self.tracker
        if not tr or self.sim == "ac":
            return {"available": False, "status": "Oponentes em tempo real só no ACC (o AC não expõe os outros carros)."}
        with tr.lock:
            cars = tr.standings()
            me = tr.cars.get(tr.player)
            out = {"available": True, "status": tr.status, "session": tr.session.get("type"), "rows": [],
                   "map_version": tr.map_version, "mirror": tr.mirror, "cars_xy": []}
            if not cars:
                return out
            now = tr.clock()
            near = {me.position - 1, me.position + 1} if me and me.position else set()
            for c in cars:
                if c.x is None:
                    continue
                x, y = c.pos_at(now)
                out["cars_xy"].append({"num": c.num, "x": round(x, 1), "y": round(y, 1), "me": c is me, "pit": c.in_pit,
                                       "near": c.position in near, "pos": c.position})
            leader = cars[0]
            my_pace = (me.pace() if me else None) or self._green_pace()
            gaps = {}
            for c in cars:
                g = tr.gap(me, c) if me and c is not me else 0.0
                gaps[c.idx] = g
                lead_laps = leader.progress - c.progress
                gl = tr.gap(c, leader) if c is not leader else 0.0
                p = c.pace()
                out["rows"].append({
                    "pos": c.position, "num": c.num, "name": c.driver, "laps": c.laps, "me": c is me,
                    "gap_me": None if g is None else round(g, 2),
                    "gap_leader": None if gl is None else round(gl, 2), "laps_down": int(lead_laps) if lead_laps >= 1 else 0,
                    "last": fmt_ms(c.last_ms), "best": fmt_ms(c.best_ms), "pit": c.in_pit, "pits": c.pits,
                    "pace_diff": round(p - my_pace, 2) if p and my_pace and c is not me else None,
                })
            # projeção de retorno após o pit
            if me:
                loss = self.pit_loss or CFG["pit_loss_s"]
                ahead, behind = None, None
                newpos = 1
                for c in cars:
                    g = gaps.get(c.idx)
                    if c is me or g is None:
                        continue
                    after = g + loss  # depois do pit, cada carro fica "loss" segundos mais à frente
                    if after > 0:
                        newpos += 1
                        if ahead is None or after < ahead[1]:
                            ahead = (c, after)
                    elif behind is None or after > behind[1]:
                        behind = (c, after)
                out["rejoin"] = {"pos": newpos, "loss": round(loss, 1), "measured": self.pit_loss is not None,
                                 "ahead": {"num": ahead[0].num, "name": ahead[0].driver, "gap": round(ahead[1], 1)} if ahead else None,
                                 "behind": {"num": behind[0].num, "name": behind[0].driver, "gap": round(-behind[1], 1)} if behind else None}
                by_pos = {c.position: c for c in cars}
                for rel, pos in (("ahead", me.position - 1), ("behind", me.position + 1)):
                    c = by_pos.get(pos)
                    if c:
                        g = gaps.get(c.idx)
                        p = c.pace()
                        out[rel] = {"num": c.num, "name": c.driver, "pos": c.position, "gap": None if g is None else round(abs(g), 2),
                                    "pace_diff": round(p - my_pace, 2) if p and my_pace else None, "pit": c.in_pit,
                                    "last": fmt_ms(c.last_ms)}
            return out

    # ------------------------------------------------------------ assistente de setup
    def _setup_advice(self):
        tips = []
        f = self.last_frame
        if not f:
            return tips
        names = ["DE", "DD", "TE", "TD"]
        warm = self.warm_s > 90 and all(self.tyre["core"]) and min(self.tyre["core"]) > 50
        p = self.tyre["p"]
        if warm and all(p):
            if self.sim in ("acc", "demo"):
                lo, hi = CFG["acc_wet_psi"] if f["rain_tyres"] else CFG["acc_dry_psi"]
            else:
                lo, hi = CFG["ac_psi"]
            mid = (lo + hi) / 2
            for axle, idx in (("dianteiros", (0, 1)), ("traseiros", (2, 3))):
                avg = sum(p[k] for k in idx) / 2
                if avg > hi or avg < lo:
                    d = mid - avg
                    verb = "Reduza" if d < 0 else "Aumente"
                    tips.append(dict(prio=1, area="Pneus", title=f"Pressão {axle} {'alta' if d < 0 else 'baixa'}",
                                     text=f"{verb} ~{abs(d):.1f} psi a frio nos {axle}. Quente: {avg:.1f} psi (alvo {lo:.1f}–{hi:.1f}).",
                                     evidence=" / ".join(f"{names[k]} {p[k]:.1f}" for k in idx)))
        ti, tm, to = self.tyre["i"], self.tyre["m"], self.tyre["o"]
        if warm and all(ti) and all(to) and all(tm) and any(abs(ti[k] - to[k]) > 0.5 for k in range(4)):
            lo, hi = CFG["camber_spread"]
            for axle, idx in (("dianteiro", (0, 1)), ("traseiro", (2, 3))):
                spread = sum(ti[k] - to[k] for k in idx) / 2
                crown = sum(tm[k] - (ti[k] + to[k]) / 2 for k in idx) / 2
                ev = " / ".join(f"{names[k]} I{ti[k]:.0f} M{tm[k]:.0f} E{to[k]:.0f}" for k in idx)
                if spread > hi:
                    tips.append(dict(prio=2, area="Pneus", title=f"Camber {axle} excessivo",
                                     text=f"Interno {spread:.0f}°C mais quente que o externo. Reduza o camber negativo {axle} (~0.2–0.3°).", evidence=ev))
                elif spread < lo:
                    tips.append(dict(prio=2, area="Pneus", title=f"Falta camber {axle}",
                                     text=f"Interno só {spread:.0f}°C acima do externo. Aumente o camber negativo {axle} (~0.2–0.3°).", evidence=ev))
                if crown > 3 and self.sim == "ac":
                    tips.append(dict(prio=2, area="Pneus", title=f"Centro do pneu {axle} quente",
                                     text="Meio mais quente que as bordas = pressão alta. Baixe a pressão.", evidence=ev))
                elif crown < -3 and self.sim == "ac":
                    tips.append(dict(prio=2, area="Pneus", title=f"Centro do pneu {axle} frio",
                                     text="Meio mais frio que as bordas = pressão baixa. Suba a pressão.", evidence=ev))
        core = self.tyre["core"]
        if warm and all(core):
            lo, hi = CFG["core_temp"]
            avg = sum(core) / 4
            if avg < lo:
                tips.append(dict(prio=3, area="Pneus", title="Pneus frios",
                                 text=f"Média {avg:.0f}°C (janela {lo:.0f}–{hi:.0f}). Feche dutos/mais camber, ou aqueça mais forte na volta de saída.", evidence=""))
            elif avg > hi:
                tips.append(dict(prio=2, area="Pneus", title="Pneus superaquecendo",
                                 text=f"Média {avg:.0f}°C. Reduza escorregamento (TC/acelerador mais suave) e verifique pressão/camber.", evidence=""))
        bh = self.brake_hot
        if all(bh):
            for axle, idx, (lo, hi) in (("dianteiros", (0, 1), CFG["brake_temp_front"]), ("traseiros", (2, 3), CFG["brake_temp_rear"])):
                avg = sum(bh[k] for k in idx) / 2
                if avg > hi:
                    tips.append(dict(prio=2, area="Freios", title=f"Freios {axle} quentes",
                                     text=f"~{avg:.0f}°C em frenagem (limite {hi:.0f}). Abra os dutos de freio {axle}.", evidence=""))
                elif avg < lo and self.warm_s > 120:
                    tips.append(dict(prio=3, area="Freios", title=f"Freios {axle} frios",
                                     text=f"~{avg:.0f}°C em frenagem (mín. {lo:.0f}). Feche os dutos de freio {axle}.", evidence=""))
        U, O = CFG["balance_under"], CFG["balance_over"]
        rules = {
            ("entrada", "under"): ("Subesterço na entrada", "Bias de freio 0.5–1% para trás, barra dianteira mais macia, ou solte o freio mais progressivamente (trail braking)."),
            ("entrada", "over"): ("Traseira solta na entrada", "Bias de freio 0.5–1% para frente, mais preload no diferencial, ou menos freio-motor."),
            ("meio", "under"): ("Subesterço no meio da curva", "Barra dianteira mais macia ou traseira mais dura; mais rake (traseira mais alta); mais camber negativo na dianteira."),
            ("meio", "over"): ("Sobresterço no meio da curva", "Barra traseira mais macia ou dianteira mais dura; menos rake."),
            ("saida", "under"): ("Subesterço na saída", "Menos preload no diferencial, barra traseira mais dura, ou dianteira mais macia."),
            ("saida", "over"): ("Traseira solta na saída", "TC +1, barra/molas traseiras mais macias, mais asa traseira; acelerador mais progressivo."),
        }
        for (phase, kind), (title, text) in rules.items():
            vals = []
            for spd in ("lenta", "rapida"):
                m, cnt = self.bal.get(f"{phase}_{spd}", (0, 0))
                if cnt > 400 and ((kind == "under" and m > U) or (kind == "over" and m < O)):
                    vals.append((spd, m))
            if vals:
                where = " e ".join("curvas lentas" if s == "lenta" else "curvas rápidas" for s, _ in vals)
                extra = ""
                if all(s == "rapida" for s, _ in vals):
                    extra = " Em alta: mais asa dianteira / menos traseira." if kind == "under" else " Em alta: mais asa traseira / menos rake."
                tips.append(dict(prio=2, area="Equilíbrio", title=f"{title} ({where})", text=text + extra,
                                 evidence="índice " + ", ".join(f"{m:+.2f}" for _, m in vals)))
        recent = [l for l in self.laps[-3:] if l["complete"]]
        if recent:
            n = len(recent)
            locks = sum(l["locks"] for l in recent) / n
            spins = sum(l["spins"] for l in recent) / n
            longc = sum(l.get("long_coasts", 0) for l in recent) / n
            early = sum(l.get("early_gas", 0) for l in recent) / n
            if locks >= 2:
                tips.append(dict(prio=1, area="Freios", title="Travamentos frequentes",
                                 text=f"{locks:.1f} por volta. Bias 0.5% para trás{', ABS +1' if self.sim == 'acc' else ''}, e reduza a pressão no pico da frenagem.", evidence=""))
            if spins >= 2:
                tips.append(dict(prio=1, area="Tração", title="Patinando nas saídas",
                                 text=f"{spins:.1f} por volta. TC +1 ou mapa de motor mais suave; abra o acelerador progressivamente.", evidence=""))
            if early >= 1.5:
                tips.append(dict(prio=1, area="Técnica", title="Acelerando cedo demais",
                                 text=f"{early:.1f} curvas por volta em que você acelera e precisa aliviar. Espere até poder começar a abrir o volante; "
                                      "se o carro parece lento antes do ápice, provavelmente freou demais na entrada.", evidence="Dion von Moltke / NASA Speed News"))
            if longc >= 2:
                tips.append(dict(prio=2, area="Técnica", title="Rolagem longa entre freio e acelerador",
                                 text=f"{longc:.1f} curvas por volta. Uma pausa curta para o carro girar é normal (meio carro em grampos, "
                                      "alguns carros em curvas rápidas); mais que isso é tempo perdido. Leve o freio mais perto do ápice "
                                      "(trail braking) ou use acelerador parcial (10–20%) para manter a velocidade.", evidence="Blayze / Driver61"))
        valid = [l["ms"] for l in self.laps[-6:] if l["valid"] and l["complete"] and not l.get("pit")]
        if len(valid) >= 4:
            sd = statistics.pstdev(valid) / 1000
            if sd > 0.6:
                tips.append(dict(prio=3, area="Técnica", title="Consistência",
                                 text=f"Desvio de {sd:.2f}s entre voltas. Foque em repetir pontos de frenagem antes de buscar tempo.", evidence=""))
        tips.sort(key=lambda t: t["prio"])
        return tips

    # ------------------------------------------------------------ snapshot para a UI
    def snapshot(self):
        with self.lock:
            f = self.last_frame
            out = {"connected": self.connected and f is not None,
                   "messages": list(self.messages)[-12:] + list(self.spot_msgs)[-3:],
                   "ref_version": self.ref_version, "spot": self.spot}
            if not f:
                if self.tracker:
                    out["race"] = self._race_view()
                return out
            b = min(self.n - 1, max(0, int(f["pos"] * self.n)))
            delta = None
            ref_ms = self.ref["ms"] if self.ref and not self.ref_provisional else 0
            if ref_ms and self.lap and self.lap.clean and f["cur_ms"] > 0:
                x = f["pos"] * self.n
                i = min(self.n - 2, int(x))
                rt = self.ref["t"][i] + (self.ref["t"][i + 1] - self.ref["t"][i]) * (x - i)
                delta = (f["cur_ms"] - rt) / 1000.0
            nxt = None
            if self.corners:
                for c in sorted(self.corners, key=lambda c: ((c.brake_bin or c.start) - b) % self.n):
                    target = c.brake_bin if c.brake_bin is not None else c.start
                    dist = ((target - b) % self.n) * self.bin_m
                    if c.contains(b) and (c.brake_bin is None or b >= c.brake_bin):
                        continue
                    if dist < 450:
                        nxt = {"id": c.id, "dist": round(dist), "ref_min": round(c.ref_min), "ref_exit": round(c.ref_exit),
                               "ref_gear": c.ref_gear, "braking": c.brake_bin is not None, "kind": c.kind,
                               "ref_coast": round(c.ref_ph["coast_s"], 2) if c.ref_ph else None,
                               "lift": self.fuel_save and c.ref_brake_kmh is not None and c.ref_brake_kmh > 180,
                               "last_brake": c.last_brake,
                               "ref_peak": round(c.ref_brake["peak"] * 100) if c.ref_brake else None,
                               "last": c.last}
                    break
            cur_c = next((c for c in self.corners if c.contains(b)), None)
            focus = []
            for c in self.corners:
                if len(c.history) >= 2:
                    focus.append({"id": c.id, "avg": round(sum(c.history) / len(c.history), 3),
                                  "last": c.last, "ref_min": round(c.ref_min), "ref_gear": c.ref_gear})
            focus.sort(key=lambda x: -x["avg"])
            step = max(1, self.n // 400)
            cur_trace = [None if v is None else round(v) for v in self.lap.speed[::step]] if self.lap else []
            tyres = []
            for k in range(4):
                tyres.append({"p": round(f["pressure"][k], 2), "core": round(f["core"][k], 1),
                              "i": round(f["t_in"][k]), "m": round(f["t_mid"][k]), "o": round(f["t_out"][k]),
                              "brake": round(f["brake_temp"][k])})
            sim = self.sim
            psi = CFG["acc_wet_psi"] if f["rain_tyres"] else CFG["acc_dry_psi"]
            strat = self._strategy(f)
            out.update({
                "sim": sim, "car": self.car, "track": self.track, "track_len": round(self.track_len),
                "session": f["session"], "is_race": f["session"] == 2,
                "live": {"speed": round(f["speed"]), "gear": f["gear"], "rpm": f["rpm"], "max_rpm": f["max_rpm"],
                         "gas": round(f["gas"], 3), "brake": round(f["brake"], 3), "steer": round(f["steer"], 3),
                         "lap": f["lap"] + 1, "pos": round(f["pos"], 4), "cur_ms": f["cur_ms"],
                         "cur": fmt_ms(f["cur_ms"]), "last": fmt_ms(f["last_ms"]), "best": fmt_ms(ref_ms or f["best_ms"]),
                         "delta": None if delta is None else round(delta, 3),
                         "predicted": fmt_ms(ref_ms + delta * 1000) if delta is not None else None,
                         "ideal": fmt_ms(ref_ms - self._ideal_gain() * 1000) if ref_ms and self._ideal_gain() > 0.01 else None,
                         "valid": f["valid"], "fuel": round(f["fuel"], 1), "fuel_per_lap": strat.get("per_lap") or 0,
                         "fuel_laps": strat.get("runs_out_in"),
                         "tc": f["tc_level"], "abs": f["abs_level"], "tc_on": f["tc_active"], "abs_on": f["abs_active"],
                         "bias": round(f["bias"] * 100, 1) if f["bias"] else None, "in_pit": f["in_pit"],
                         "where": f"T{cur_c.id}" if cur_c else "", "status": f["status"]},
                "tyres": tyres, "psi_target": psi if sim in ("acc", "demo") else CFG["ac_psi"],
                "core_target": CFG["core_temp"], "has_imo": any(t["i"] for t in tyres),
                "next": nxt, "focus": focus[:5], "laps": self.laps[-12:][::-1], "setup": self.setup_tips,
                "balance": {k: round(v[0], 3) for k, v in self.bal.items() if v[1] > 100},
                "cur_trace": cur_trace, "provisional": self.ref_provisional,
                "corners": [{"id": c.id, "start": c.start / self.n, "apex": c.apex / self.n, "end": c.end / self.n} for c in self.corners],
                "strategy": strat, "race": self._race_view(),
            })
            return out

    def ref_payload(self):
        with self.lock:
            if not self.ref:
                return {"version": self.ref_version, "speed": [], "brake": [], "gas": []}
            step = max(1, self.n // 400)
            return {"version": self.ref_version, "ms": self.ref["ms"], "provisional": self.ref_provisional,
                    "speed": [round(v) for v in self.ref["speed"][::step]],
                    "brake": [round(v, 2) for v in self.ref["brake"][::step]],
                    "gas": [round(v, 2) for v in self.ref["gas"][::step]]}

    def clear_reference(self):
        with self.lock:
            path = os.path.join(DATA_DIR, "referencias", self.key() + ".json")
            try:
                os.remove(path)
            except OSError:
                pass
            self.ref = None
            self.ref_provisional = True
            self.corners = []
            self.ref_version += 1
            self.say("info", "ref", "Referência apagada. A próxima volta limpa vira a nova referência.", speak=False)
