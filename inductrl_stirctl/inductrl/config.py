"""설정 로드·검증·해시와 live 시작 조건 점검."""
import copy
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from . import decode
from .models import (ALL_CHANNELS, ANALOG_CHANNELS, CH_ROTATION, MATERIAL_CHANNELS,
                     MATERIALS)

REGISTER_TYPES = ("holding", "input")
FEEDBACK_REGISTER_TYPES = ("coil", "discrete_input", "holding", "input")
WORD_ORDERS = ("big", "little")
COUNTER_BITS = (16, 32)

MATERIAL_THRESHOLD_KEYS = {
    "saltwater": ("conductivity_min", "conductivity_max", "conductivity_max_range"),
    "caco3": ("turbidity_top_min", "turbidity_top_max", "turbidity_bottom_min",
              "turbidity_bottom_max", "turbidity_max_range", "top_bottom_max_diff"),
}

ACTUATOR_LIVE_KEYS = ("stop_coil_address", "stop_value", "stop_confirm_timeout_s",
                      "stopped_rpm_max", "stop_hold_seconds")


class ConfigError(ValueError):
    """설정 구조 오류."""

    def __init__(self, errors: List[str]) -> None:
        super().__init__("설정 오류:\n  - " + "\n  - ".join(errors))
        self.errors = errors


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ConfigError(["최상위 JSON은 객체여야 합니다"])
    return cfg


def canonical_json(cfg: Dict[str, Any]) -> str:
    return json.dumps(cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def config_hash(cfg: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(cfg).encode("utf-8")).hexdigest()


def deep_copy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(cfg)


# --------------------------------------------------------------------------- #
# 형 검사 도우미
# --------------------------------------------------------------------------- #
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(errors: List[str], d: Dict[str, Any], key: str, path: str, nullable: bool,
         min_value: Optional[float] = None, min_exclusive: bool = False,
         max_value: Optional[float] = None) -> None:
    if key not in d:
        errors.append("{}.{} 항목이 없습니다".format(path, key))
        return
    v = d[key]
    if v is None:
        if not nullable:
            errors.append("{}.{} 는 null일 수 없습니다".format(path, key))
        return
    if not _is_number(v):
        errors.append("{}.{} 는 숫자여야 합니다 (현재: {!r})".format(path, key, v))
        return
    if min_value is not None:
        if (min_exclusive and v <= min_value) or (not min_exclusive and v < min_value):
            errors.append("{}.{} 는 {}{} 이어야 합니다 (현재: {})".format(
                path, key, ">" if min_exclusive else ">=", min_value, v))
    if max_value is not None and v > max_value:
        errors.append("{}.{} 는 <= {} 이어야 합니다 (현재: {})".format(path, key, max_value, v))


def _bool(errors: List[str], d: Dict[str, Any], key: str, path: str) -> None:
    if key not in d:
        errors.append("{}.{} 항목이 없습니다".format(path, key))
    elif not isinstance(d[key], bool):
        errors.append("{}.{} 는 true/false 여야 합니다".format(path, key))


def _choice(errors: List[str], d: Dict[str, Any], key: str, path: str,
            choices: Tuple[Any, ...], nullable: bool = True) -> None:
    if key not in d:
        errors.append("{}.{} 항목이 없습니다".format(path, key))
        return
    v = d[key]
    if v is None:
        if not nullable:
            errors.append("{}.{} 는 null일 수 없습니다".format(path, key))
        return
    if isinstance(v, bool) or v not in choices:
        errors.append("{}.{} 는 {} 중 하나여야 합니다 (현재: {!r})".format(path, key, choices, v))


def _address(errors: List[str], d: Dict[str, Any], key: str, path: str) -> None:
    if key not in d:
        errors.append("{}.{} 항목이 없습니다".format(path, key))
        return
    v = d[key]
    if v is None:
        return
    if not _is_int(v) or not 0 <= v <= 65535:
        errors.append("{}.{} 는 0..65535 정수 또는 null 이어야 합니다".format(path, key))


# --------------------------------------------------------------------------- #
# 구조 검증
# --------------------------------------------------------------------------- #
def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """구조·형 오류 목록을 돌려줍니다. 미확인 값(null)은 오류가 아닙니다."""
    errors = []  # type: List[str]
    if not isinstance(cfg.get("database"), str) or not cfg.get("database"):
        errors.append("database 경로(문자열)가 필요합니다")
    if not isinstance(cfg.get("host"), str) or not cfg.get("host"):
        errors.append("host(문자열)가 필요합니다")
    _num(errors, cfg, "port", "", False, 1, False, 65535)
    _num(errors, cfg, "unit_id", "", False, 0, False, 247)
    for key in ("port", "unit_id"):
        if key in cfg and cfg[key] is not None and _is_number(cfg[key]) and not _is_int(cfg[key]):
            errors.append("{} 는 정수여야 합니다".format(key))
    _num(errors, cfg, "read_timeout_s", "", False, 0, True, 30)
    _num(errors, cfg, "write_timeout_s", "", False, 0, True, 30)
    _num(errors, cfg, "cycle_budget_s", "", False, 0, True, 60)

    channels = cfg.get("channels")
    if not isinstance(channels, dict):
        errors.append("channels 객체가 필요합니다")
    else:
        for name in ALL_CHANNELS:
            ch = channels.get(name)
            if not isinstance(ch, dict):
                errors.append("channels.{} 객체가 필요합니다".format(name))
                continue
            errors.extend(_validate_channel(name, ch))

    decision = cfg.get("decision")
    if not isinstance(decision, dict):
        errors.append("decision 객체가 필요합니다")
    else:
        errors.extend(_validate_decision(decision))
        interval = decision.get("interval_seconds")
        budget = cfg.get("cycle_budget_s")
        if _is_number(interval) and _is_number(budget) and budget > interval:
            errors.append("cycle_budget_s({})는 decision.interval_seconds({}) 이하여야 합니다".format(
                budget, interval))

    actuator = cfg.get("actuator")
    if not isinstance(actuator, dict):
        errors.append("actuator 객체가 필요합니다")
    else:
        p = "actuator"
        _bool(errors, actuator, "live_enabled", p)
        _bool(errors, actuator, "verified", p)
        _address(errors, actuator, "stop_coil_address", p)
        if "stop_value" not in actuator:
            errors.append("actuator.stop_value 항목이 없습니다")
        elif actuator["stop_value"] is not None and not isinstance(actuator["stop_value"], bool):
            errors.append("actuator.stop_value 는 true/false/null 이어야 합니다 (정지값을 가정하지 마세요)")
        _num(errors, actuator, "stop_confirm_timeout_s", p, True, 0, True)
        _num(errors, actuator, "stopped_rpm_max", p, True, 0)
        _num(errors, actuator, "stop_hold_seconds", p, True, 0)

    fb = cfg.get("stop_feedback")
    if fb is not None:
        if not isinstance(fb, dict):
            errors.append("stop_feedback 는 객체 또는 생략이어야 합니다")
        else:
            p = "stop_feedback"
            _choice(errors, fb, "register_type", p, FEEDBACK_REGISTER_TYPES)
            _address(errors, fb, "address", p)
            if "stopped_value" not in fb:
                errors.append("stop_feedback.stopped_value 항목이 없습니다")
            elif fb["stopped_value"] is not None and not (
                    isinstance(fb["stopped_value"], bool) or _is_int(fb["stopped_value"])):
                errors.append("stop_feedback.stopped_value 는 true/false/정수/null 이어야 합니다")
            _bool(errors, fb, "verified", p)
            if fb.get("verified") is True:
                for key in ("register_type", "address", "stopped_value"):
                    if fb.get(key) is None:
                        errors.append("stop_feedback.verified=true 인데 {} 가 null 입니다".format(key))
    return errors


def _validate_channel(name: str, ch: Dict[str, Any]) -> List[str]:
    errors = []  # type: List[str]
    p = "channels." + name
    _address(errors, ch, "address_zero_based", p)
    _choice(errors, ch, "register_type", p, REGISTER_TYPES)
    _choice(errors, ch, "data_type", p, tuple(decode.DATA_TYPE_REGISTERS.keys()))
    _choice(errors, ch, "word_order", p, WORD_ORDERS)
    _num(errors, ch, "scale", p, True)
    if ch.get("scale") == 0:
        errors.append(p + ".scale 은 0일 수 없습니다")
    _num(errors, ch, "offset", p, True)
    if "unit" not in ch:
        errors.append(p + ".unit 항목이 없습니다")
    elif ch["unit"] is not None and not isinstance(ch["unit"], str):
        errors.append(p + ".unit 은 문자열 또는 null 이어야 합니다")
    _num(errors, ch, "valid_min", p, True)
    _num(errors, ch, "valid_max", p, True)
    vmin, vmax = ch.get("valid_min"), ch.get("valid_max")
    if _is_number(vmin) and _is_number(vmax) and vmin >= vmax:
        errors.append(p + ".valid_min 은 valid_max 보다 작아야 합니다")
    _num(errors, ch, "stale_seconds", p, False, 0, True)
    _bool(errors, ch, "mapping_verified", p)

    addr, dtype = ch.get("address_zero_based"), ch.get("data_type")
    if _is_int(addr) and dtype in decode.DATA_TYPE_REGISTERS:
        if addr + decode.register_count(dtype) > 65536:
            errors.append(p + " 주소 범위가 65535를 넘습니다")

    if name == CH_ROTATION:
        _num(errors, ch, "pulses_per_rev", p, True, 0, True)
        _choice(errors, ch, "counter_bits", p, COUNTER_BITS)
        _num(errors, ch, "rpm_window_seconds", p, True, 0, True)
        _num(errors, ch, "max_rpm", p, True, 0, True)
        bits = ch.get("counter_bits")
        if bits is not None and dtype is not None:
            expected = "uint16" if bits == 16 else "uint32"
            if bits in COUNTER_BITS and dtype != expected:
                errors.append("{}.data_type 은 counter_bits={} 에 맞게 {} 이어야 합니다".format(
                    p, bits, expected))

    if ch.get("mapping_verified") is True:
        for key in missing_mapping_fields(name, ch):
            errors.append("{}.mapping_verified=true 인데 {} 가 null 입니다".format(p, key))
    return errors


def missing_mapping_fields(name: str, ch: Dict[str, Any]) -> List[str]:
    """읽고 해석하는 데 필요한데 null인 항목."""
    keys = ["address_zero_based", "register_type", "data_type"]
    if ch.get("data_type") in decode.DATA_TYPE_REGISTERS and decode.register_count(ch["data_type"]) == 2:
        keys.append("word_order")
    if name in ANALOG_CHANNELS:
        keys += ["scale", "offset"]
    if name == CH_ROTATION:
        keys += ["pulses_per_rev", "counter_bits", "rpm_window_seconds", "max_rpm"]
    return [k for k in keys if ch.get(k) is None]


def _validate_decision(dec: Dict[str, Any]) -> List[str]:
    errors = []  # type: List[str]
    p = "decision"
    _num(errors, dec, "interval_seconds", p, False, 0.1, False, 60)
    _num(errors, dec, "window_seconds", p, False, 0, True)
    _num(errors, dec, "min_stir_seconds", p, False, 0)
    _num(errors, dec, "hold_seconds", p, False, 0)
    _num(errors, dec, "min_coverage", p, False, 0, True, 1)
    _num(errors, dec, "running_rpm_min", p, True, 0)
    _num(errors, dec, "running_confirm_seconds", p, False, 0)
    _bool(errors, dec, "thresholds_verified", p)
    if _is_number(dec.get("interval_seconds")) and _is_number(dec.get("window_seconds")):
        if dec["window_seconds"] < dec["interval_seconds"]:
            errors.append("decision.window_seconds 는 interval_seconds 이상이어야 합니다")
    for material in MATERIALS:
        block = dec.get(material)
        if not isinstance(block, dict):
            errors.append("decision.{} 객체가 필요합니다".format(material))
            continue
        for key in MATERIAL_THRESHOLD_KEYS[material]:
            is_spread = key.endswith("_range") or key.endswith("_diff")
            _num(errors, block, key, "decision." + material, True, 0 if is_spread else None)
        pairs = ([("conductivity_min", "conductivity_max")] if material == "saltwater" else
                 [("turbidity_top_min", "turbidity_top_max"),
                  ("turbidity_bottom_min", "turbidity_bottom_max")])
        for lo, hi in pairs:
            if _is_number(block.get(lo)) and _is_number(block.get(hi)) and block[lo] > block[hi]:
                errors.append("decision.{}.{} 가 {} 보다 큽니다".format(material, lo, hi))
    return errors


# --------------------------------------------------------------------------- #
# 의미 점검
# --------------------------------------------------------------------------- #
def required_channels(material: str) -> Tuple[str, ...]:
    """판정에 필요한 채널: 재료 채널 + 회전 카운터(가동 확인용)."""
    return MATERIAL_CHANNELS[material] + (CH_ROTATION,)


def channel_is_mapped(name: str, ch: Dict[str, Any]) -> bool:
    return (ch.get("address_zero_based") is not None and ch.get("mapping_verified") is True
            and not missing_mapping_fields(name, ch))


def unset_thresholds(cfg: Dict[str, Any], material: str) -> List[str]:
    block = cfg["decision"][material]
    return ["decision.{}.{}".format(material, k) for k in MATERIAL_THRESHOLD_KEYS[material]
            if block.get(k) is None]


def unverified_reasons(cfg: Dict[str, Any], material: str) -> List[str]:
    """완료 후보를 '미검증 판정'으로 표시해야 하는 사유."""
    reasons = []  # type: List[str]
    if cfg["decision"].get("thresholds_verified") is not True:
        reasons.append("decision.thresholds_verified=false")
    reasons.extend("{} = null".format(k) for k in unset_thresholds(cfg, material))
    return reasons


def check_live_readiness(cfg: Dict[str, Any], material: str, arm_stop: bool) -> List[str]:
    """live 모드 시작에 빠진 조건 목록. 빈 목록이면 시작 가능."""
    missing = []  # type: List[str]
    for name in required_channels(material):
        ch = cfg["channels"][name]
        if ch.get("mapping_verified") is not True:
            missing.append("channels.{}.mapping_verified=true 필요".format(name))
        for key in missing_mapping_fields(name, ch):
            missing.append("channels.{}.{} 확인 필요(null)".format(name, key))
    dec = cfg["decision"]
    if dec.get("thresholds_verified") is not True:
        missing.append("decision.thresholds_verified=true 필요")
    for key in unset_thresholds(cfg, material):
        missing.append("{} 확인 필요(null)".format(key))
    if dec.get("running_rpm_min") is None:
        missing.append("decision.running_rpm_min 확인 필요(null)")
    act = cfg["actuator"]
    if act.get("live_enabled") is not True:
        missing.append("actuator.live_enabled=true 필요")
    if act.get("verified") is not True:
        missing.append("actuator.verified=true 필요")
    for key in ACTUATOR_LIVE_KEYS:
        if act.get(key) is None:
            missing.append("actuator.{} 확인 필요(null)".format(key))
    if not arm_stop:
        missing.append("실행 인자 --arm-stop 필요")
    return missing
