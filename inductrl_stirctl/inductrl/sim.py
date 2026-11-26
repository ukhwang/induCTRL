"""실시간 시뮬레이터의 교반 물리 모델.

웹앱에서 '물(L) + 용질(g)'만 넣고 실험을 재현하기 위한 계산입니다.
Modbus 읽기/쓰기 코드는 없습니다(가상 값 계산 전용). actual 쓰기는 actuator.py 에만 있습니다.

모델 요약 (기준: 10 L 물, 50 g/L 투입, 300 RPM)
  농도       c    = 용질(g) / 물(L)                                  [g/L]
  부하계수   Lf   = (c / 50)^0.55                                     (1.0 = 기준 투입)
  부상 임계  Njs  = 190 · Lf                                          [RPM] — 이보다 느리면 침전물이 뜨지 않음
  혼합 시상수 τ   = 14 s · (V/10)^0.35 · Lf^0.5
  혼합 진행  dM/dt = rate(rpm) · (1-M) / τ,  rate = 0.9·(rpm/Njs)^1.4  (rpm ≥ 0.75·Njs)
  축동력     P    = 120 W · (V/10)^0.67 · (1+0.35(Lf-1)) · (rpm/300)^2.85 · (1+0.22(1-M))
  에너지     E    = ∫ P dt                                            [Wh]
  잔여 불균일 CV  = 12% (염수) · 18% (탄산칼슘) × (1-M)                [%]

회전수 정책(--control-mode)
  fixed : 고정 회전수로 고정 시간 동안 계속 운전(자동 정지 없음) → 비교 기준 에너지
  rule  : 고정 회전수, 판정 엔진의 안정 조건이 서면 정지
  ai    : 초기 부하 진단 → 부상 단계 → 감속 순환. 같은 판정 조건을 더 낮은 축동력으로 만족
"""
import math
import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# --- 기준값 -----------------------------------------------------------------
REF_CONCENTRATION_G_PER_L = 50.0
REF_VOLUME_L = 10.0
NJS_BASE_RPM = 190.0
TAU_MIX_S = 14.0
P_REF_W = 120.0              # 10 L, 부하 1.0, 300 RPM, 완전 혼합
SUSPEND_RATIO = 0.75         # Njs 의 75% 미만이면 침전 상태 유지
TAU_SETTLE_S = 25.0          # 임펠러 정지 후 침강 시상수

FIXED_RPM = 400.0
FIXED_SECONDS = 120.0
RULE_RPM = 320.0
AI_DIAG_RPM = 260.0
AI_BURST_RATIO = 1.50
AI_CRUISE_RATIO = 1.15
AI_BLEND_S = 1.5

CV_SCALE = {"saltwater": 12.0, "caco3": 18.0}

CONTROL_MODES = ("fixed", "rule", "ai")
LIMITS = {
    "water_l": (0.5, 200.0),
    "solute_g": (0.0, 20000.0),
    "speed_factor": (0.25, 50.0),
    "fixed_seconds": (10.0, 3600.0),
}


@dataclass(frozen=True)
class SimParams:
    """실험 조건. CLI/웹에서 들어온 값을 그대로 담습니다."""

    water_l: float = 10.0
    solute_g: float = 500.0
    control_mode: str = "rule"
    speed_factor: float = 1.0
    fixed_seconds: float = FIXED_SECONDS

    @property
    def concentration(self) -> float:
        return self.solute_g / self.water_l if self.water_l > 0 else 0.0

    @property
    def load(self) -> float:
        conc = self.concentration
        return min(2.5, max(0.25, (conc / REF_CONCENTRATION_G_PER_L) ** 0.55))

    @property
    def njs_rpm(self) -> float:
        return NJS_BASE_RPM * self.load

    @property
    def tau_mix_s(self) -> float:
        return TAU_MIX_S * (self.water_l / REF_VOLUME_L) ** 0.35 * math.sqrt(self.load)

    def as_dict(self) -> Dict[str, float]:
        return {
            "water_l": self.water_l,
            "solute_g": self.solute_g,
            "concentration_g_per_l": round(self.concentration, 3),
            "control_mode": self.control_mode,
            "speed_factor": self.speed_factor,
            "fixed_seconds": self.fixed_seconds,
        }


def validate(params: SimParams) -> List[str]:
    """잘못된 실험 조건을 한국어 메시지로 돌려줍니다(빈 목록이면 정상)."""
    problems = []  # type: List[str]
    for name, value in (("water_l", params.water_l), ("solute_g", params.solute_g),
                        ("speed_factor", params.speed_factor),
                        ("fixed_seconds", params.fixed_seconds)):
        lo, hi = LIMITS[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            problems.append("{} 값이 숫자가 아닙니다".format(name))
        elif value < lo or value > hi:
            problems.append("{} 는 {} ~ {} 범위여야 합니다(입력: {})".format(name, lo, hi, value))
    if params.control_mode not in CONTROL_MODES:
        problems.append("control_mode 는 fixed/rule/ai 중 하나여야 합니다(입력: {})".format(
            params.control_mode))
    return problems


def power_w(rpm: float, water_l: float, load: float, mix: float) -> float:
    """축동력(W). 회전수^2.85, 부하(점도), 체적, 미혼합 상태의 추가 부하를 반영합니다."""
    if rpm <= 0:
        return 0.0
    volume = (water_l / REF_VOLUME_L) ** 0.67
    load_term = 1.0 + 0.35 * (load - 1.0)
    return P_REF_W * volume * load_term * (rpm / 300.0) ** 2.85 * (1.0 + 0.22 * (1.0 - mix))


def mix_rate(rpm: float, njs: float) -> float:
    """혼합 진행률(무차원). 부상 임계 미만이면 0(침전물이 뜨지 않음)."""
    if rpm < SUSPEND_RATIO * njs or njs <= 0:
        return 0.0
    return min(3.0, 0.9 * (rpm / njs) ** 1.4)


def ai_setpoint(tau: float, njs: float) -> float:
    """AI 회전수 정책: 진단 → 부상(단기 고회전) → 감속 순환. 단계 전환은 선형으로 잇습니다."""
    steps = (
        (0.0, AI_DIAG_RPM),
        (6.0, min(420.0, AI_BURST_RATIO * njs)),
        (14.0, max(120.0, AI_CRUISE_RATIO * njs)),
    )
    value = steps[0][1]
    for index in range(1, len(steps)):
        start, level = steps[index]
        if tau >= start:
            previous = steps[index - 1][1]
            blend = min(1.0, (tau - start) / AI_BLEND_S)
            value = previous + (level - previous) * blend
    return value


def command_rpm(params: SimParams, tau: float) -> float:
    if params.control_mode == "fixed":
        return FIXED_RPM
    if params.control_mode == "rule":
        return RULE_RPM
    return ai_setpoint(tau, params.njs_rpm)


def phase_of(params: SimParams, tau: float) -> str:
    if params.control_mode == "fixed":
        return "FIXED"
    if params.control_mode == "rule":
        return "HOLD"
    if tau < 6.0:
        return "DIAGNOSE"
    if tau < 14.0:
        return "SUSPEND"
    return "CRUISE"


def fixed_baseline_energy_wh(params: SimParams, step_s: float = 0.5) -> float:
    """같은 투입량에서 FIXED 기준 운전(고정 회전·고정 시간·자동 정지 없음)의 모델 계산 에너지."""
    steps = max(1, int(round(params.fixed_seconds / step_s)))
    mix = 0.0
    energy = 0.0
    rpm = 0.0
    for index in range(steps):
        t = index * step_s
        target = FIXED_RPM if t >= 1.0 else 0.0
        rpm = min(target, target * max(0.0, (t - 1.0)) / 2.0) if t >= 1.0 else 0.0
        mix = _step_mix(mix, rpm, params, step_s)
        energy += power_w(rpm, params.water_l, params.load, mix) * step_s / 3600.0
    return energy


def _step_mix(mix: float, rpm: float, params: SimParams, dt: float) -> float:
    rate = mix_rate(rpm, params.njs_rpm)
    if rate > 0:
        mix += rate * (1.0 - mix) * dt / params.tau_mix_s
    else:
        mix -= mix * dt / TAU_SETTLE_S
    return min(1.0, max(0.0, mix))


class SimPhysics:
    """가상 플랜트의 혼합 상태·동력·에너지를 시간에 따라 적분합니다."""

    def __init__(self, params: SimParams, material: str, origin: float,
                 band: Callable[[str], Tuple[float, float, Optional[float]]],
                 rng: Optional[random.Random] = None) -> None:
        self.params = params
        self.material = material
        self.origin = float(origin)
        self.band = band
        self.rng = rng or random.Random(11)
        self.mix = 0.0
        self.energy_wh = 0.0
        self.last_t = float(origin)
        self.stalled = False
        self.stopped = False
        self.cv_pct = self._cv()

    # --- 적분 ---------------------------------------------------------- #
    def advance(self, t: float, rpm_at: Callable[[float], float]) -> None:
        if t <= self.last_t:
            return
        dt = t - self.last_t
        rpm_mid = rpm_at(self.last_t + dt / 2.0)
        njs = self.params.njs_rpm
        # 기동 램프(tau<8s)는 제외하고, 모터가 돌고 있는데도 부상 임계 미만이면 '침전 유지'로 기록
        if (not self.stopped and t - self.origin > 8.0 and rpm_mid > 0.3 * njs
                and mix_rate(rpm_mid, njs) <= 0.0):
            self.stalled = True
        self.mix = _step_mix(self.mix, rpm_mid, self.params, dt)
        self.energy_wh += power_w(rpm_mid, self.params.water_l, self.params.load,
                                  self.mix) * dt / 3600.0
        self.last_t = t
        self.cv_pct = self._cv()

    def mark_stopped(self) -> None:
        self.stopped = True

    def _cv(self) -> float:
        return CV_SCALE.get(self.material, 12.0) * (1.0 - self.mix)

    # --- 값 ------------------------------------------------------------ #
    def rpm_command(self, tau: float) -> float:
        return command_rpm(self.params, tau)

    def phase(self, tau: float) -> str:
        return phase_of(self.params, tau)

    def _noise(self, rpm: float, amp: float) -> float:
        scale = 0.5 + 0.5 * min(1.0, max(0.0, rpm / 300.0))
        return self.rng.uniform(-1.0, 1.0) * amp * scale

    def analog_value(self, channel: str, tau: float, rpm: float) -> float:
        """공학 단위 가상 센서값. 미혼합 상태가 상·하부 차이로 나타납니다."""
        unmixed = 1.0 - self.mix
        material = self.material
        if channel == "conductivity":
            lo, hi, _span = self.band(channel)
            mid, width = (lo + hi) / 2.0, max(1e-6, hi - lo)
            if material == "saltwater":
                return mid + 0.6 * width * unmixed + self._noise(rpm, 0.06)
            return 7.0 + 2.0 * unmixed + self._noise(rpm, 0.05)
        if channel in ("turbidity_top", "turbidity_bottom"):
            lo, hi, _span = self.band(channel)
            mid, width = (lo + hi) / 2.0, max(1e-6, hi - lo)
            if material == "caco3":
                sign = -1.0 if channel == "turbidity_top" else 1.0
                return mid + sign * 0.85 * width * unmixed + self._noise(rpm, 0.8)
            sign = -1.0 if channel == "turbidity_top" else 1.0
            return 45.0 + sign * 8.0 * unmixed + self._noise(rpm, 0.4)
        raise KeyError("알 수 없는 아날로그 채널: " + channel)

    def metrics(self, t: float, rpm: float) -> Dict[str, float]:
        tau = t - self.origin
        phase = "STALLED" if self.stalled else self.phase(tau)
        return {
            "elapsed_s": tau,
            "rpm": rpm,
            "rpm_setpoint": self.rpm_command(tau),
            "phase": phase,
            "power_w": power_w(rpm, self.params.water_l, self.params.load, self.mix),
            "energy_wh": self.energy_wh,
            "cv_pct": self.cv_pct,
            "mix": self.mix,
            "njs_rpm": self.params.njs_rpm,
        }
