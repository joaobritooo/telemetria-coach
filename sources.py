"""
Fontes de telemetria.

- ACC (Assetto Corsa Competizione) e AC (Assetto Corsa) expõem a telemetria via
  shared memory do Windows: Local\\acpmf_physics, Local\\acpmf_graphics, Local\\acpmf_static.
  O layout do ACC é uma extensão do layout do AC, então a mesma struct serve para ambos
  (campos que o AC não tem ficam zerados).
- DemoSource gera telemetria sintética para testar o app sem o jogo aberto.

Todas as fontes devolvem um dicionário "frame" normalizado (ver make_frame).
"""
import ctypes
import math
import random
import subprocess
import sys
import time

c_f = ctypes.c_float
c_i = ctypes.c_int


def W(n):
    return ctypes.c_wchar * n


def F(n):
    return c_f * n


class Physics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_i), ("gas", c_f), ("brake", c_f), ("fuel", c_f), ("gear", c_i), ("rpms", c_i),
        ("steerAngle", c_f), ("speedKmh", c_f), ("velocity", F(3)), ("accG", F(3)),
        ("wheelSlip", F(4)), ("wheelLoad", F(4)), ("wheelsPressure", F(4)), ("wheelAngularSpeed", F(4)),
        ("tyreWear", F(4)), ("tyreDirtyLevel", F(4)), ("tyreCoreTemperature", F(4)), ("camberRAD", F(4)),
        ("suspensionTravel", F(4)), ("drs", c_f), ("tc", c_f), ("heading", c_f), ("pitch", c_f), ("roll", c_f),
        ("cgHeight", c_f), ("carDamage", F(5)), ("numberOfTyresOut", c_i), ("pitLimiterOn", c_i), ("abs", c_f),
        ("kersCharge", c_f), ("kersInput", c_f), ("autoShifterOn", c_i), ("rideHeight", F(2)),
        ("turboBoost", c_f), ("ballast", c_f), ("airDensity", c_f), ("airTemp", c_f), ("roadTemp", c_f),
        ("localAngularVel", F(3)), ("finalFF", c_f), ("performanceMeter", c_f), ("engineBrake", c_i),
        ("ersRecoveryLevel", c_i), ("ersPowerLevel", c_i), ("ersHeatCharging", c_i), ("ersIsCharging", c_i),
        ("kersCurrentKJ", c_f), ("drsAvailable", c_i), ("drsEnabled", c_i), ("brakeTemp", F(4)), ("clutch", c_f),
        ("tyreTempI", F(4)), ("tyreTempM", F(4)), ("tyreTempO", F(4)), ("isAIControlled", c_i),
        ("tyreContactPoint", F(12)), ("tyreContactNormal", F(12)), ("tyreContactHeading", F(12)),
        ("brakeBias", c_f), ("localVelocity", F(3)), ("P2PActivations", c_i), ("P2PStatus", c_i),
        ("currentMaxRpm", c_i), ("mz", F(4)), ("fx", F(4)), ("fy", F(4)), ("slipRatio", F(4)),
        ("slipAngle", F(4)), ("tcinAction", c_i), ("absInAction", c_i), ("suspensionDamage", F(4)),
        ("tyreTemp", F(4)), ("waterTemp", c_f), ("brakePressure", F(4)), ("frontBrakeCompound", c_i),
        ("rearBrakeCompound", c_i), ("padLife", F(4)), ("discLife", F(4)),
    ]


_GFX_COMMON = [
    ("packetId", c_i), ("status", c_i), ("session", c_i),
    ("currentTime", W(15)), ("lastTime", W(15)), ("bestTime", W(15)), ("split", W(15)),
    ("completedLaps", c_i), ("position", c_i), ("iCurrentTime", c_i), ("iLastTime", c_i), ("iBestTime", c_i),
    ("sessionTimeLeft", c_f), ("distanceTraveled", c_f), ("isInPit", c_i), ("currentSectorIndex", c_i),
    ("lastSectorTime", c_i), ("numberOfLaps", c_i), ("tyreCompound", W(33)), ("replayTimeMultiplier", c_f),
    ("normalizedCarPosition", c_f),
]


class GraphicsACC(ctypes.Structure):
    _pack_ = 4
    _fields_ = _GFX_COMMON + [
        ("activeCars", c_i), ("carCoordinates", F(180)), ("carID", c_i * 60), ("playerCarID", c_i),
        ("penaltyTime", c_f), ("flag", c_i), ("penalty", c_i), ("idealLineOn", c_i), ("isInPitLane", c_i),
        ("surfaceGrip", c_f), ("mandatoryPitDone", c_i), ("windSpeed", c_f), ("windDirection", c_f),
        ("isSetupMenuVisible", c_i), ("mainDisplayIndex", c_i), ("secondaryDisplayIndex", c_i),
        ("TC", c_i), ("TCCut", c_i), ("EngineMap", c_i), ("ABS", c_i), ("fuelXLap", c_f),
        ("rainLights", c_i), ("flashingLights", c_i), ("lightsStage", c_i), ("exhaustTemperature", c_f),
        ("wiperLV", c_i), ("DriverStintTotalTimeLeft", c_i), ("DriverStintTimeLeft", c_i), ("rainTyres", c_i),
        ("sessionIndex", c_i), ("usedFuel", c_f), ("deltaLapTime", W(15)), ("iDeltaLapTime", c_i),
        ("estimatedLapTime", W(15)), ("iEstimatedLapTime", c_i), ("isDeltaPositive", c_i), ("iSplit", c_i),
        ("isValidLap", c_i), ("fuelEstimatedLaps", c_f), ("trackStatus", W(33)), ("missingMandatoryPits", c_i),
        ("Clock", c_f), ("directionLightsLeft", c_i), ("directionLightsRight", c_i), ("GlobalYellow", c_i),
        ("GlobalYellow1", c_i), ("GlobalYellow2", c_i), ("GlobalYellow3", c_i), ("GlobalWhite", c_i),
        ("GlobalGreen", c_i), ("GlobalChequered", c_i), ("GlobalRed", c_i), ("mfdTyreSet", c_i),
        ("mfdFuelToAdd", c_f), ("mfdTyrePressureLF", c_f), ("mfdTyrePressureRF", c_f),
        ("mfdTyrePressureLR", c_f), ("mfdTyrePressureRR", c_f),
    ]


class GraphicsAC(ctypes.Structure):
    _pack_ = 4
    _fields_ = _GFX_COMMON + [
        ("carCoordinates", F(3)), ("penaltyTime", c_f), ("flag", c_i), ("idealLineOn", c_i),
        ("isInPitLane", c_i), ("surfaceGrip", c_f),
    ]


class Static(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", W(15)), ("acVersion", W(15)), ("numberOfSessions", c_i), ("numCars", c_i),
        ("carModel", W(33)), ("track", W(33)), ("playerName", W(33)), ("playerSurname", W(33)),
        ("playerNick", W(33)), ("sectorCount", c_i), ("maxTorque", c_f), ("maxPower", c_f), ("maxRpm", c_i),
        ("maxFuel", c_f), ("suspensionMaxTravel", F(4)), ("tyreRadius", F(4)), ("maxTurboBoost", c_f),
        ("deprecated_1", c_f), ("deprecated_2", c_f), ("penaltiesEnabled", c_i), ("aidFuelRate", c_f),
        ("aidTireRate", c_f), ("aidMechanicalDamage", c_f), ("aidAllowTyreBlankets", c_i), ("aidStability", c_f),
        ("aidAutoClutch", c_i), ("aidAutoBlip", c_i), ("hasDRS", c_i), ("hasERS", c_i), ("hasKERS", c_i),
        ("kersMaxJ", c_f), ("engineBrakeSettingsCount", c_i), ("ersPowerControllerCount", c_i),
        ("trackSPlineLength", c_f), ("trackConfiguration", W(33)), ("ersMaxJ", c_f), ("isTimedRace", c_i),
        ("hasExtraLap", c_i), ("carSkin", W(33)), ("reversedGridPositions", c_i), ("PitWindowStart", c_i),
        ("PitWindowEnd", c_i), ("isOnline", c_i),
    ]


# ---------------------------------------------------------------- Windows shared memory
if sys.platform == "win32":
    import ctypes.wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenFileMappingW.restype = wt.HANDLE
    _k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    _k32.MapViewOfFile.restype = ctypes.c_void_p
    _k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
    _k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wt.HANDLE]

    class _MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD), ("Protect", wt.DWORD),
                    ("Type", wt.DWORD)]

    _k32.VirtualQuery.restype = ctypes.c_size_t
    _k32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t]
FILE_MAP_READ = 0x0004


class SharedMem:
    def __init__(self, name):
        self.name = name
        self.handle = None
        self.ptr = None
        self.size = 0

    def open(self):
        h = _k32.OpenFileMappingW(FILE_MAP_READ, False, self.name)
        if not h:
            return False
        p = _k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not p:
            _k32.CloseHandle(h)
            return False
        mbi = _MBI()
        _k32.VirtualQuery(p, ctypes.byref(mbi), ctypes.sizeof(mbi))
        self.handle, self.ptr, self.size = h, p, mbi.RegionSize
        return True

    def read(self, cls):
        obj = cls()  # zerado: campos além do tamanho real ficam 0
        ctypes.memmove(ctypes.addressof(obj), self.ptr, min(ctypes.sizeof(cls), self.size))
        return obj

    def close(self):
        if self.ptr:
            _k32.UnmapViewOfFile(self.ptr)
        if self.handle:
            _k32.CloseHandle(self.handle)
        self.handle = self.ptr = None


def running_processes():
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}
    except Exception:
        return set()


ACC_EXE = {"ac2-win64-shipping.exe"}
AC_EXE = {"acs.exe", "acs_x86.exe"}


def detect_sim():
    procs = running_processes()
    if procs & ACC_EXE:
        return "acc"
    if procs & AC_EXE:
        return "ac"
    return None


# ---------------------------------------------------------------- frame normalizado
def make_frame(**kw):
    """Frame normalizado consumido pelo motor de análise."""
    base = dict(
        t=time.time(), sim="", car="", track="", track_len=0.0, status=2, session=0,
        pos=0.0, lap=0, cur_ms=0, last_ms=0, best_ms=0, sector=0, valid=True, in_pit=False,
        speed=0.0, gas=0.0, brake=0.0, steer=0.0, gear=1, rpm=0, max_rpm=8000, clutch=0.0,
        acc_lat=0.0, acc_long=0.0, yaw=0.0, wheel_w=[0.0] * 4, slip_angle=[0.0] * 4, slip_ratio=[0.0] * 4,
        pressure=[0.0] * 4, core=[0.0] * 4, t_in=[0.0] * 4, t_mid=[0.0] * 4, t_out=[0.0] * 4,
        brake_temp=[0.0] * 4, fuel=0.0, max_fuel=0.0, tyres_out=0, tc_level=0, abs_level=0,
        tc_active=False, abs_active=False, bias=0.0, compound="", air=0.0, road=0.0, rain_tyres=False,
        # corrida / estratégia
        session_left_ms=0, total_laps=0, timed=True, extra_lap=False, pit_missing=0, mfd_fuel=None,
    )
    base.update(kw)
    return base


class SharedMemorySource:
    """Lê ACC ou AC via shared memory. sim: 'acc', 'ac' ou None (autodetecta)."""

    def __init__(self, sim=None):
        self.forced = sim
        self.sim = sim
        self.phys = SharedMem("Local\\acpmf_physics")
        self.gfx = SharedMem("Local\\acpmf_graphics")
        self.stat = SharedMem("Local\\acpmf_static")
        self.opened = False
        self.last_packet = None
        self.last_packet_change = 0.0
        self.next_try = 0.0
        self.static = None
        self.static_read_at = 0.0

    def _open(self):
        if self.forced is None:
            self.sim = detect_sim()
            if self.sim is None:
                return False
        if self.phys.open() and self.gfx.open() and self.stat.open():
            self.opened = True
            self.last_packet_change = time.time()
            return True
        for m in (self.phys, self.gfx, self.stat):
            m.close()
        return False

    def _close(self):
        for m in (self.phys, self.gfx, self.stat):
            m.close()
        self.opened = False
        self.static = None

    def read(self):
        now = time.time()
        if not self.opened:
            if now < self.next_try:
                return None
            self.next_try = now + 2.0
            if not self._open():
                return None
        p = self.phys.read(Physics)
        g = self.gfx.read(GraphicsACC if self.sim == "acc" else GraphicsAC)
        if self.static is None or now - self.static_read_at > 5:
            self.static = self.stat.read(Static)
            self.static_read_at = now
        s = self.static

        if p.packetId != self.last_packet:
            self.last_packet = p.packetId
            self.last_packet_change = now
        elif now - self.last_packet_change > 5 and g.status == 0:
            # jogo fechado: solta o mapeamento e tenta reabrir depois
            self._close()
            return None

        is_acc = self.sim == "acc"
        valid = bool(g.isValidLap) if is_acc else p.numberOfTyresOut <= 2
        max_rpm = p.currentMaxRpm or s.maxRpm or 8000
        return make_frame(
            t=now, sim=self.sim, car=s.carModel, track=(s.track + (" " + s.trackConfiguration if s.trackConfiguration else "")).strip(),
            track_len=s.trackSPlineLength, status=g.status, session=g.session,
            pos=g.normalizedCarPosition, lap=g.completedLaps, cur_ms=g.iCurrentTime, last_ms=g.iLastTime,
            best_ms=g.iBestTime, sector=g.currentSectorIndex, valid=valid,
            in_pit=bool(g.isInPit or g.isInPitLane),
            speed=p.speedKmh, gas=p.gas, brake=p.brake, steer=p.steerAngle, gear=p.gear - 1, rpm=p.rpms,
            max_rpm=max_rpm, clutch=p.clutch, acc_lat=p.accG[0], acc_long=p.accG[2], yaw=p.localAngularVel[1],
            wheel_w=list(p.wheelAngularSpeed), slip_angle=list(p.slipAngle), slip_ratio=list(p.slipRatio),
            pressure=list(p.wheelsPressure), core=list(p.tyreCoreTemperature),
            t_in=list(p.tyreTempI), t_mid=list(p.tyreTempM), t_out=list(p.tyreTempO),
            brake_temp=list(p.brakeTemp), fuel=p.fuel, max_fuel=s.maxFuel, tyres_out=p.numberOfTyresOut,
            tc_level=getattr(g, "TC", 0), abs_level=getattr(g, "ABS", 0),
            tc_active=bool(p.tcinAction), abs_active=bool(p.absInAction), bias=p.brakeBias,
            compound=g.tyreCompound, air=p.airTemp, road=p.roadTemp,
            rain_tyres=bool(getattr(g, "rainTyres", 0)),
            session_left_ms=max(0, int(g.sessionTimeLeft)) if g.sessionTimeLeft < 1e9 else 0,
            total_laps=g.numberOfLaps, timed=bool(s.isTimedRace) or g.numberOfLaps <= 0,
            extra_lap=bool(s.hasExtraLap), pit_missing=g.missingMandatoryPits if is_acc and 0 < g.missingMandatoryPits < 10 else 0,
            mfd_fuel=round(g.mfdFuelToAdd, 1) if is_acc and 0 <= g.mfdFuelToAdd < 300 else None,
        )


# ---------------------------------------------------------------- modo demo
class DemoSource:
    """
    Simula um carro GT3 numa pista fictícia de ~4.2 km para testar o app.
    O "piloto" comete erros aleatórios (freia cedo/tarde, trava roda, patina, coasting)
    e o carro tem tendência de subesterço em curva lenta + pressões dianteiras altas,
    para que o coach e o assistente de setup tenham o que dizer.
    """
    # (posição do ápice em m, raio m, direção)
    CORNERS = [(420, 28, 1), (900, 90, -1), (1350, 160, 1), (1700, 45, -1), (2250, 220, 1),
               (2700, 35, 1), (3150, 70, -1), (3700, 120, 1), (4000, 300, -1)]
    L = 4200.0

    RACE_MS = 30 * 60 * 1000

    def __init__(self, speed_factor=1.0, field=None):
        self.k = speed_factor
        self.field = field  # DemoField (oponentes simulados), opcional
        self.n = int(self.L)
        self.s = 50.0
        self.v = 30.0
        self.lap = 0
        self.lap_t = 0.0
        self.last_ms = 0
        self.best_ms = 0
        self.fuel = 70.0
        self.core = [40.0] * 4
        self.btemp = [150.0] * 4
        self.last_real = time.time()
        self.sim_time = 0.0
        self.curv = self._curvature()
        self._new_lap_plan()

    def _curvature(self):
        curv = [0.0] * self.n
        for apex, r, d in self.CORNERS:
            half = int(min(160, r * 1.2))
            for k in range(-half, half + 1):
                i = int(apex + k) % self.n
                w = 0.5 * (1 + math.cos(math.pi * k / half))
                curv[i] = max(curv[i], w / r) if d > 0 else min(curv[i], -w / r)
        return curv

    def _new_lap_plan(self):
        self.mistakes = {}
        for ci in range(len(self.CORNERS)):
            r = random.random()
            if r < 0.15:
                self.mistakes[ci] = "early"
            elif r < 0.27:
                self.mistakes[ci] = "late"
            elif r < 0.35:
                self.mistakes[ci] = "coast"
            elif r < 0.43:
                self.mistakes[ci] = "spin"
            elif r < 0.50:
                self.mistakes[ci] = "lift"
            elif r < 0.57:
                self.mistakes[ci] = "push"   # sai de frente no meio da curva
            elif r < 0.63:
                self.mistakes[ci] = "loose"  # traseira escapa na entrada
        self.skill = random.uniform(0.95, 1.0)
        self.profile = self._speed_profile()
        self.lap_valid = random.random() > 0.12

    def _speed_profile(self):
        n = self.n
        vmax = 72.0
        lim = [vmax] * n
        for i in range(n):
            c = abs(self.curv[i])
            if c > 1e-4:
                lim[i] = min(vmax, math.sqrt(1.65 * 9.81 / c) * self.skill)
        # erros: alteram a velocidade de ápice e o ponto de frenagem
        brake_dec = [13.5] * n
        for ci, kind in self.mistakes.items():
            apex = self.CORNERS[ci][0]
            if kind == "early":
                for k in range(-60, 1):
                    lim[int(apex + k) % n] *= 0.93
            if kind == "late":
                for k in range(-150, -40):
                    brake_dec[int(apex + k) % n] = 17.0
                for k in range(-30, 30):
                    lim[int(apex + k) % n] *= 0.92
        v = lim[:]
        for _ in range(2):  # passes circulares
            for i in range(2 * n - 1, -1, -1):  # frenagem
                a, b = i % n, (i + 1) % n
                v[a] = min(v[a], math.sqrt(v[b] ** 2 + 2 * brake_dec[a]))
            for i in range(2 * n):  # aceleração
                a, b = i % n, (i + 1) % n
                acc = max(1.2, 9.0 - v[a] / 9.0)
                v[b] = min(v[b], math.sqrt(v[a] ** 2 + 2 * acc))
        return v

    def _corner_at(self, s):
        for ci, (apex, r, d) in enumerate(self.CORNERS):
            dd = (s - apex + self.L / 2) % self.L - self.L / 2
            if -170 < dd < 90:
                return ci, dd
        return None, 0

    def read(self):
        now = time.time()
        dt = min(0.1, now - self.last_real) * self.k
        self.last_real = now
        steps = max(1, int(dt / 0.005))
        h = dt / steps
        for _ in range(steps):
            i = int(self.s) % self.n
            target = self.profile[i]
            self.v += max(-15.0, min(9.0, (target - self.v) * 8.0)) * h
            self.s += self.v * h
            self.lap_t += h
            self.sim_time += h
            if self.s >= self.L:
                self.s -= self.L
                self.lap += 1
                self.last_ms = int(self.lap_t * 1000)
                if self.lap_valid and (self.best_ms == 0 or self.last_ms < self.best_ms):
                    self.best_ms = self.last_ms
                self.lap_t = 0.0
                self.fuel -= 2.85
                if self.fuel < 5:
                    self.fuel = 70.0
                self._new_lap_plan()

        i = int(self.s) % self.n
        nxt = self.profile[(i + 8) % self.n]
        dv = nxt - self.v
        ci, dd = self._corner_at(self.s)
        kind = self.mistakes.get(ci) if ci is not None else None
        gas = brake = 0.0
        if dv < -0.6:
            brake = min(1.0, -dv / 4.0)
        elif dv > 0.05 or abs(self.curv[i]) < 1e-4:
            gas = min(1.0, 0.35 + dv * 1.5) if abs(self.curv[i]) > 1e-4 else 1.0
        else:
            gas = 0.25
        if kind == "coast" and -25 < dd < 30:
            gas = brake = 0.0  # rola tempo demais entre soltar o freio e acelerar
        if kind == "lift" and brake == 0 and -260 < dd < -120:
            gas = 0.0  # tira o pé antes da frenagem
        curv = self.curv[i]
        lat_g = self.v ** 2 * curv / 9.81
        speed = self.v * 3.6
        gear = max(1, min(6, int(speed / 45) + 1))
        rpm = int(3500 + (speed % 45) / 45 * 4800)

        # rodas: raio 0.34 m
        w = [self.v / 0.34] * 4
        if brake > 0.6 and (kind == "late" and -70 < dd < -20):
            w[0] *= 0.55  # trava dianteira esquerda
        if kind == "spin" and 15 < dd < 70 and gas > 0.4:
            w[2] *= 1.25
            w[3] *= 1.22
        # balanço: subesterço em curva lenta, neutro/leve sobre em rápida
        base = abs(lat_g) * 2.2
        under = 1.35 if speed < 130 else 0.95
        sa = [base * under, base * under, base, base]
        sa = [x * random.uniform(0.95, 1.05) for x in sa]
        yaw = self.v * curv  # rotação "normal" = acompanha a curva
        if kind == "push" and -20 < dd < 25:
            sa[0] *= 1.9; sa[1] *= 1.9
        if (kind == "loose" and brake > 0.2 and -60 < dd < -5) or (kind == "spin" and 15 < dd < 60 and gas > 0.4):
            sa[2] *= 2.2; sa[3] *= 2.2
            yaw *= 1.6

        work = abs(lat_g) * 0.5 + brake * 0.6 + gas * 0.2
        for k in range(4):
            target = 60 + work * 30 + (8 if k < 2 else 0)
            self.core[k] += (target - self.core[k]) * 0.01
            bt = 180 + brake * (900 if k < 2 else 550)
            self.btemp[k] += (bt - self.btemp[k]) * (0.03 if brake > 0 else 0.004)
        pressure = [21.8 + (c - 20) * 0.075 + (0.6 if k < 2 else 0) for k, c in enumerate(self.core)]
        core = self.core
        t_mid = [c + 2 for c in core]
        t_in = [c + 9 for c in core]
        t_out = [c - 3 for c in core]

        left = max(0, self.RACE_MS - int(self.sim_time * 1000))
        if self.field:
            self.field.step(self.sim_time, self.lap + self.s / self.L, speed, left)
        return make_frame(
            session_left_ms=left, total_laps=0, timed=True, pit_missing=1,
            t=now, sim="demo", car="Demo GT3", track="Autódromo Demo", track_len=self.L, status=2,
            session=2, pos=self.s / self.L, lap=self.lap, cur_ms=int(self.lap_t * 1000),
            last_ms=self.last_ms, best_ms=self.best_ms, sector=min(2, int(self.s / self.L * 3)),
            valid=self.lap_valid or self.s < self.L * 0.6, in_pit=False, speed=speed, gas=gas, brake=brake,
            steer=max(-1, min(1, curv * 18)), gear=gear, rpm=rpm, max_rpm=8300, acc_lat=lat_g,
            acc_long=-brake * 1.4 + gas * 0.5, yaw=yaw, wheel_w=w, slip_angle=sa, pressure=pressure,
            core=list(core), t_in=t_in, t_mid=t_mid, t_out=t_out, brake_temp=list(self.btemp),
            fuel=self.fuel, max_fuel=120, tyres_out=0 if self.lap_valid or self.s < self.L * 0.6 else 3,
            tc_level=3, abs_level=2, tc_active=kind == "spin" and gas > 0.4 and 15 < dd < 70,
            abs_active=brake > 0.7, bias=0.565, compound="dry_compound", air=24, road=31,
        )
