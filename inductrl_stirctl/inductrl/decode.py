"""Modbus 레지스터 해석.

- 레지스터 한 개(16비트) 안의 바이트 순서는 Modbus 표준(빅 엔디언)으로 가정합니다.
- word_order 는 32비트 값에서 두 레지스터의 순서입니다.
  big    = 첫 레지스터가 상위 워드 (ABCD)
  little = 첫 레지스터가 하위 워드 (CDAB)
"""
import math
import struct
from typing import List, Optional, Sequence, Union

Number = Union[int, float]

DATA_TYPE_REGISTERS = {
    "uint16": 1,
    "int16": 1,
    "uint32": 2,
    "int32": 2,
    "float32": 2,
}


class DecodeError(ValueError):
    pass


def register_count(data_type: str) -> int:
    if data_type not in DATA_TYPE_REGISTERS:
        raise DecodeError("지원하지 않는 data_type: {!r}".format(data_type))
    return DATA_TYPE_REGISTERS[data_type]


def decode_registers(registers: Sequence[int], data_type: str, word_order: Optional[str]) -> Number:
    count = register_count(data_type)
    if len(registers) != count:
        raise DecodeError("{} 는 레지스터 {}개가 필요합니다 (수신 {}개)".format(
            data_type, count, len(registers)))
    for r in registers:
        if isinstance(r, bool) or not isinstance(r, int) or not 0 <= r <= 0xFFFF:
            raise DecodeError("레지스터 값이 0..65535 정수가 아닙니다: {!r}".format(r))
    if count == 1:
        raw = registers[0]
        if data_type == "int16" and raw >= 0x8000:
            return raw - 0x10000
        return raw
    if word_order == "big":
        hi, lo = registers[0], registers[1]
    elif word_order == "little":
        hi, lo = registers[1], registers[0]
    else:
        raise DecodeError("32비트 값에는 word_order(big/little)가 필요합니다")
    raw32 = (hi << 16) | lo
    if data_type == "uint32":
        return raw32
    if data_type == "int32":
        return raw32 - 0x100000000 if raw32 >= 0x80000000 else raw32
    value = struct.unpack(">f", struct.pack(">I", raw32))[0]
    if math.isnan(value) or math.isinf(value):
        raise DecodeError("float32 값이 NaN/Inf 입니다 (raw=0x{:08X})".format(raw32))
    return value


def to_engineering(raw: Number, scale: float, offset: float) -> float:
    return float(raw) * float(scale) + float(offset)


# --- 가상(mock) 장치·테스트용 역변환 ------------------------------------------
def engineering_to_raw(value: float, data_type: str, scale: float, offset: float) -> Number:
    raw = (value - offset) / scale
    if data_type == "float32":
        return raw
    return int(round(raw))


def encode_registers(raw: Number, data_type: str, word_order: Optional[str]) -> List[int]:
    count = register_count(data_type)
    if count == 1:
        iv = int(raw)
        if data_type == "uint16":
            iv = max(0, min(0xFFFF, iv))
        else:
            iv = max(-0x8000, min(0x7FFF, iv)) & 0xFFFF
        return [iv]
    if data_type == "float32":
        raw32 = struct.unpack(">I", struct.pack(">f", float(raw)))[0]
    elif data_type == "uint32":
        raw32 = int(raw) & 0xFFFFFFFF
    else:
        raw32 = max(-0x80000000, min(0x7FFFFFFF, int(raw))) & 0xFFFFFFFF
    hi, lo = (raw32 >> 16) & 0xFFFF, raw32 & 0xFFFF
    if word_order == "little":
        return [lo, hi]
    return [hi, lo]
