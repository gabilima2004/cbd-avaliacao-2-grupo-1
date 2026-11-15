"""Registro fixo de 128 bytes: três int64 little-endian + 104 bytes de preenchimento."""
import struct

RECORD = 128
_FMT = struct.Struct('<qqq')


def encode(a, b, c):
    return _FMT.pack(a, b, c) + bytes(RECORD - _FMT.size)


def decode(raw):
    return _FMT.unpack_from(raw, 0)


def field(raw, campo):
    return struct.unpack_from('<q', raw, 8 * campo)[0]
