"""Trim excess number precision in PDF content streams (paths and colours only).

Design tools (Figma, Sketch, some Illustrator exports) write every coordinate with 6 decimals.
Path coordinates are rounded to 0.01 pt (~0.0035 mm, far below any printer dot) and colour
values to 3 decimals (finer than 8-bit colour). Everything else, including text, strings,
matrices, dash patterns and inline images, is left byte-for-byte as it was.
"""
from __future__ import annotations

import re

PATH_OPS = {b"m", b"l", b"c", b"v", b"y", b"re"}
COLOR_OPS = {b"rg", b"RG", b"g", b"G", b"k", b"K", b"sc", b"SC", b"scn", b"SCN"}
WIDTH_OPS = {b"w"}

_DELIM = b"()<>[]{}/%"
_WS = b" \t\r\n\f\x00"
_NUM = re.compile(rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)")
_TOKEN = re.compile(rb"[^ \t\r\n\f\x00()<>\[\]{}/%]+")


def _fmt(val: float, decimals: int) -> bytes:
    s = f"{val:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s.encode()


def trim(data: bytes, path_decimals: int = 2, color_decimals: int = 3) -> bytes:
    """Return the stream with long numbers shortened. Raises ValueError on anything it can't parse."""
    out = bytearray()
    last = 0                     # bytes before `last` are already copied to out
    operands: list[tuple[int, int]] = []   # spans of numeric operands since the last operator
    i, n = 0, len(data)

    def flush_numbers(decimals: int):
        nonlocal last
        for s, e in operands:
            tok = data[s:e]
            if b"." not in tok:
                continue
            frac = tok.split(b".", 1)[1]
            if len(frac) <= decimals:
                continue
            new = _fmt(float(tok), decimals)
            out.extend(data[last:s])
            out.extend(new)
            last = e

    while i < n:
        ch = data[i]
        if ch in _WS:
            i += 1
            continue
        if ch == 0x25:  # % comment
            j = data.find(b"\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == 0x28:  # ( literal string, with nesting and escapes
            depth, i = 1, i + 1
            while i < n and depth:
                c = data[i]
                if c == 0x5C:
                    i += 2
                    continue
                if c == 0x28:
                    depth += 1
                elif c == 0x29:
                    depth -= 1
                i += 1
            if depth:
                raise ValueError("unterminated string")
            operands.clear()
            continue
        if ch == 0x3C:  # < hex string or << dict
            if data[i:i + 2] == b"<<":
                i += 2
            else:
                j = data.find(b">", i)
                if j < 0:
                    raise ValueError("unterminated hex string")
                i = j + 1
            operands.clear()
            continue
        if ch in b">[]{}":
            i += 1
            if ch in b"[]":
                operands.clear()
            continue
        if ch == 0x2F:  # /Name
            m = _TOKEN.match(data, i + 1)
            i = m.end() if m else i + 1
            operands.clear()
            continue
        m = _TOKEN.match(data, i)
        if not m:
            raise ValueError("unexpected byte")
        tok = m.group()
        if _NUM.fullmatch(tok):
            operands.append((m.start(), m.end()))
            i = m.end()
            continue
        # operator
        if tok in PATH_OPS:
            flush_numbers(path_decimals)
        elif tok in COLOR_OPS or tok in WIDTH_OPS:
            flush_numbers(color_decimals)
        operands.clear()
        i = m.end()
        if tok == b"ID":  # inline image: binary data until whitespace + EI + whitespace
            k = i + 1
            while True:
                k = data.find(b"EI", k)
                if k < 0:
                    raise ValueError("unterminated inline image")
                before = data[k - 1] if k > 0 else 32
                after = data[k + 2] if k + 2 < n else 32
                if before in _WS and after in _WS:
                    break
                k += 2
            i = k + 2
    out.extend(data[last:])
    return bytes(out)
