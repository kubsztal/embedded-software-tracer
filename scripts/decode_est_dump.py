#!/usr/bin/env python3
"""Decode an EST ram-block dump using the matching config.json.

The dump must contain magic sentinels + est_metadata_t + buffer[]
(typically a copy of g_est_ram_block). buffer[] alone is not supported.

Endianness of metadata and memcpy payloads is detected from the magic
pair; event-stream ID/timestamp bytes are always little-endian.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

TYPE_SIZES = {
    "u8": 1, "s8": 1,
    "u16": 2, "s16": 2,
    "u32": 4, "s32": 4,
    "u64": 8, "s64": 8,
    "f32": 4, "f64": 8
}

# struct format strings for unpacking primitive types (little-endian)
TYPE_STRUCT_FMT = {
    "u8":  "<B",
    "s8":  "<b",
    "u16": "<H",
    "s16": "<h",
    "u32": "<I",
    "s32": "<i",
    "u64": "<Q",
    "s64": "<q",
    "f32": "<f",
    "f64": "<d",
}

META_FMT = {
    "little": "<I 2B 2s",
    "big":    ">I 2B 2s",
}
META_SIZE = struct.calcsize(META_FMT["little"])  # 8 bytes


def parse_short_hash(value) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = str(value).strip()
    if text.lower().startswith("0x"):
        return int(text, 16) & 0xFFFFFFFF
    return int(text, 0) & 0xFFFFFFFF


ARRAY_RE = re.compile(r'^(u8|s8|u16|s16|u32|s32|u64|s64|f32|f64)a([1-9][0-9]*)$')
# Token with optional stringify name suffix: u8(name), f32a5(name), b3 (no suffix)
DATA_TOKEN_RE = re.compile(r'^((?:u8|s8|u16|s16|u32|s32|u64|s64|f32|f64)(?:a[1-9][0-9]*)?|b(?:[1-9]|[1-5][0-9]|6[0-4]))(\([A-Za-z0-9_]+\))?$')
# Stringify Name field: plain name or name[index_range]
STRINGIFY_NAME_RE = re.compile(
    r'^([A-Za-z0-9_]+)(?:\[(\.\.|[0-9]+|[0-9]+\.\.(?:[0-9]+)?|\.\.[0-9]+)\])?$'
)


def _parse_data_token(raw_tok: str) -> tuple[str, str | None]:
    """Split 'u8a5(foo)' -> ('u8a5', 'foo'). Returns (base, None) if no suffix."""
    m = DATA_TOKEN_RE.match(str(raw_tok).strip())
    if not m:
        return str(raw_tok).strip(), None
    suffix = m.group(2)  # '(name)' or None
    return m.group(1), suffix[1:-1] if suffix else None


def parse_bitfield_width(tok: str) -> int | None:
    """Return the bit-width for a bitfield token 'bN', or None for a plain type."""
    if tok.startswith("b") and len(tok) > 1 and tok[1:].isdigit():
        return int(tok[1:])
    return None


def parse_array_token(tok: str) -> tuple[str, int] | None:
    """Return (base_type, count) for an array token '<base>a<count>', or None."""
    m = ARRAY_RE.match(tok)
    if m:
        return m.group(1), int(m.group(2))
    return None


def _parse_num(s: str) -> float:
    """Parse a number string (decimal, hex 0x/0X, float, E-notation) to float."""
    import math
    s = s.strip()
    if s.lower().startswith('0x'):
        return float(int(s, 16))
    return float(s)


def _parse_values_field(values_str: str) -> tuple[float, float] | None:
    """Parse a Values field string to an inclusive (lo, hi) float range."""
    import math
    s = values_str.strip()
    if not s:
        return None
    if '..' in s:
        parts = s.split('..', 1)
        lo_s, hi_s = parts[0].strip(), parts[1].strip()
        try:
            lo = _parse_num(lo_s) if lo_s else -math.inf
            hi = _parse_num(hi_s) if hi_s else math.inf
        except (ValueError, OverflowError):
            return None
        if lo > hi:
            return None
        return lo, hi
    else:
        try:
            v = _parse_num(s)
        except (ValueError, OverflowError):
            return None
        return v, v


def _index_range_to_bounds(idx_range: str | None, array_size: int | None) -> tuple[int, int]:
    """Convert an index range string to (first_idx, last_idx) inclusive."""
    LARGE = 2**31 - 1
    size = array_size if array_size is not None else LARGE
    if idx_range is None:
        return 0, 0
    if idx_range == '..':
        return 0, max(0, size - 1)
    if '..' in idx_range:
        parts = idx_range.split('..', 1)
        lo_s, hi_s = parts[0].strip(), parts[1].strip()
        lo = int(lo_s) if lo_s else 0
        hi = int(hi_s) if hi_s else max(0, size - 1)
        return lo, hi
    v = int(idx_range)
    return v, v


def build_stringify_rules(stringify_rows: list[dict]) -> dict:
    """Build a stringify rules lookup from TracePointDataStringify config rows.

    Returns a dict mapping base_name -> list of (idx_range_str_or_None, val_range, fmt_str)
    where idx_range_str_or_None is the raw index range string (e.g. '3..7', '..', None).
    """
    rules: dict[str, list] = {}
    for row in stringify_rows:
        name_raw = str(row.get("Name", "")).strip()
        values_raw = str(row.get("Values", "")).strip()
        fmt_raw = str(row.get("Stringify", "")).strip()
        m = STRINGIFY_NAME_RE.match(name_raw)
        if not m:
            continue
        base_name, idx_range = m.group(1), m.group(2)
        val_range = _parse_values_field(values_raw)
        if val_range is None:
            continue
        if base_name not in rules:
            rules[base_name] = []
        rules[base_name].append((idx_range, val_range, fmt_raw))
    return rules


def _apply_stringify_format(fmt: str, value, index: int | None) -> str:
    """Apply a printf-style Stringify format string to a numeric value.

    Replaces %i with the element index (0-based decimal) and all other
    specifiers with the value. Returns the formatted string.
    """
    # Replace %i with the index first (before general specifier processing)
    if index is not None:
        result = fmt.replace('%i', str(index))
    else:
        result = fmt

    # Now apply the remaining value specifier (at most one)
    i = 0
    out = []
    while i < len(result):
        if result[i] == '%' and i + 1 < len(result):
            nxt = result[i + 1]
            if nxt == '%':
                out.append('%')
                i += 2
                continue
            # Find the full specifier
            j = i + 1
            while j < len(result) and result[j] in '-+ #0':
                j += 1
            while j < len(result) and result[j].isdigit():
                j += 1
            if j < len(result) and result[j] == '.':
                j += 1
                while j < len(result) and result[j].isdigit():
                    j += 1
            if j < len(result) and result[j] in 'diouxXeEfgGaA':
                spec = result[i:j + 1]  # e.g. '%-10d'
                conv = result[j]
                try:
                    if conv in 'di':
                        out.append(spec % int(value))
                    elif conv in 'ouxX':
                        out.append(spec % int(value))
                    else:  # f, e, E, g, G, a, A
                        out.append(spec % float(value))
                except (ValueError, TypeError):
                    out.append(spec % value)
                i = j + 1
                continue
        out.append(result[i])
        i += 1
    return ''.join(out)


def lookup_stringify(stringify_rules: dict, sname: str, value,
                     index: int | None = None, array_size: int | None = None) -> str | None:
    """Look up a stringify rule and apply it, returning the formatted string or None.

    For scalar tokens: index is None, idx_range must also be None (non-indexed rule).
    For array tokens: index is the 0-based element index, idx_range must cover it.
    """
    rules_for_name = stringify_rules.get(sname)
    if not rules_for_name:
        return None
    for (idx_range, val_range, fmt) in rules_for_name:
        is_indexed_rule = idx_range is not None
        # Check index range match
        if index is None:
            if is_indexed_rule:
                continue  # indexed rule cannot apply to scalar
        else:
            if not is_indexed_rule:
                continue  # non-indexed rule cannot apply to array element
            lo, hi = _index_range_to_bounds(idx_range, array_size)
            if not (lo <= index <= hi):
                continue
        # Check value range match
        lo_v, hi_v = val_range
        try:
            fval = float(value)
        except (TypeError, ValueError):
            continue
        if not (lo_v <= fval <= hi_v):
            continue
        # Match! Apply format.
        return _apply_stringify_format(fmt, value, index)
    return None



def pick_container_sizes(total_bits: int) -> list[int]:
    """Binary decomposition of ceil(total_bits/8) bytes into powers of {8,4,2,1},
    largest first -- mirrors pick_container_sizes() in generate_est_cfg_files.py."""
    bytes_needed = (total_bits + 7) // 8
    return [p for p in (8, 4, 2, 1) if bytes_needed & p]


def group_bitfield_run(widths: list[int]) -> list[tuple[list[int], int]]:
    """Greedy left-to-right accumulation of a bitfield run into words of <=64 bits.
    Mirrors group_bitfield_run() in generate_est_cfg_files.py."""
    words: list[tuple[list[int], int]] = []
    current: list[int] = []
    current_bits = 0
    for w in widths:
        if current_bits + w > 64:
            if current:
                words.append((list(current), current_bits))
            current, current_bits = [], 0
        current.append(w)
        current_bits += w
    if current:
        words.append((list(current), current_bits))
    return words


def assign_word_to_containers(widths: list[int]):
    """Given a closed word's field widths (sum <= 64), compute how each field's bits
    map onto the containers list. Returns (containers_bytes, per_field_parts), where
    per_field_parts[i] = [(container_index, shift_in_container, part_width, field_bit_offset), ...].
    Mirrors assign_word_to_containers() in generate_est_cfg_files.py."""
    total_bits = sum(widths)
    containers = pick_container_sizes(total_bits)

    container_starts: list[int] = []
    container_ends: list[int] = []
    cum = 0
    for c in containers:
        container_starts.append(cum)
        cum += c * 8
        container_ends.append(cum)

    # compute bit ranges for each field
    ranges: list[tuple[int, int]] = []
    pos = 0
    for w in widths:
        ranges.append((pos, pos + w))
        pos += w

    per_field_parts: list[list[tuple[int, int, int, int]]] = []
    for (start, end) in ranges:
        parts: list[tuple[int, int, int, int]] = []
        for ci in range(len(containers)):
            c_start, c_end = container_starts[ci], container_ends[ci]
            part_start, part_end = max(start, c_start), min(end, c_end)
            if part_start < part_end:
                parts.append((ci, part_start - c_start, part_end - part_start, part_start - start))
        per_field_parts.append(parts)
    return containers, per_field_parts



def unpack_payload(payload: bytes, data_tokens: list[str]) -> list[dict]:
    """Unpack raw payload bytes according to TracePointData token list.

    Returns a list of {"Type": token, "Value": decoded_value} in argument order.
    Mirrors the packing logic of est_pack_* as generated by generate_est_cfg_files.py:
      - Consecutive bitfield tokens (bN) are packed LSB-first using the binary
        decomposition container layout from pick_container_sizes / assign_word_to_containers.
      - All other tokens are laid out sequentially as little-endian values.
    """
    results: list[dict] = []
    pos = 0  # byte offset into payload

    i = 0
    n = len(data_tokens)
    while i < n:
        tok = data_tokens[i]
        arr = parse_array_token(tok)
        bw = parse_bitfield_width(tok)
        if arr is not None:
            base, count = arr
            fmt = TYPE_STRUCT_FMT[base]
            sz = TYPE_SIZES[base]
            vals = []
            for _ in range(count):
                if pos + sz > len(payload):
                    break
                (val,) = struct.unpack_from(fmt, payload, pos)
                pos += sz
                vals.append(val)
            results.append({"Value": vals})
            i += 1
        elif bw is None:
            # Ordinary primitive
            fmt = TYPE_STRUCT_FMT[tok]
            sz = TYPE_SIZES[tok]
            if pos + sz > len(payload):
                break
            (val,) = struct.unpack_from(fmt, payload, pos)
            pos += sz
            results.append({"Value": val})
            i += 1
        else:
            # Collect the bitfield run
            run_tokens: list[str] = []
            run_widths: list[int] = []
            total_bits = 0
            j = i
            while j < n:
                bw_j = parse_bitfield_width(data_tokens[j])
                if bw_j is None:
                    break
                if total_bits + bw_j > 64:
                    break
                run_tokens.append(data_tokens[j])
                run_widths.append(bw_j)
                total_bits += bw_j
                j += 1

            # group_bitfield_run splits the run into words of <=64 bits
            words = group_bitfield_run(run_widths)
            field_idx = 0
            for (word_widths, _) in words:
                containers, per_field_parts = assign_word_to_containers(word_widths)
                # Read container bytes sequentially (same order as packing)
                container_vals: list[int] = []
                for c_size in containers:
                    if pos + c_size > len(payload):
                        break
                    container_vals.append(int.from_bytes(payload[pos:pos + c_size], "little"))
                    pos += c_size

                # Reconstruct each field value from its parts
                for fi, parts in enumerate(per_field_parts):
                    val = 0
                    for (ci, shift_in_container, part_width, field_bit_offset) in parts:
                        if ci >= len(container_vals):
                            break
                        mask = (1 << part_width) - 1
                        bits = (container_vals[ci] >> shift_in_container) & mask
                        val |= bits << field_bit_offset
                    tok_bf = run_tokens[field_idx]
                    results.append({"Value": val})
                    field_idx += 1

            i = j

    return results


def compute_payload_size(data_tokens: list[str]) -> int:
    """Compute packed payload size in bytes (mirrors layout from generate_est_cfg_files.py)."""
    total = 0
    i = 0
    n = len(data_tokens)
    while i < n:
        tok = data_tokens[i]
        arr = parse_array_token(tok)
        bw = parse_bitfield_width(tok)
        if arr is not None:
            base, count = arr
            total += TYPE_SIZES[base] * count
            i += 1
        elif bw is None:
            total += TYPE_SIZES[tok]
            i += 1
        else:
            # Collect the run
            run_widths: list[int] = []
            j = i
            run_bits = 0
            while j < n:
                bw_j = parse_bitfield_width(data_tokens[j])
                if bw_j is None:
                    break
                if run_bits + bw_j > 64:
                    break
                run_widths.append(bw_j)
                run_bits += bw_j
                j += 1
            # Use group_bitfield_run + pick_container_sizes to get exact byte count
            for (word_widths, _) in group_bitfield_run(run_widths):
                total += sum(pick_container_sizes(sum(word_widths)))
            i = j
    return total


def load_id_info(config: dict) -> dict[int, dict]:
    gen = config.get("General", {})
    prefix = gen.get("TracerPrefix", "TRC_")
    name_underscored = gen.get("TracePointNameUnderscored", False)
    # Build stringify rules from config
    stringify_rules = build_stringify_rules(config.get("TracePointDataStringify", []))
    mapping: dict[int, dict] = {}
    for tp in config.get("TracePoints", []):
        group = tp.get("TracePointGroup") or ""
        name = tp.get("TracePointName") or ""
        tp_type = tp.get("TracePointType") or ""
        inst_size = tp.get("TracePointInstanceSize") or ""
        inst_bytes = 1 if inst_size == "1B" else (2 if inst_size == "2B" else 0)
        core_str = tp.get("TracePointCore") or ""
        core_static: int | None = None
        core_bytes = 0
        if core_str in ("1B", "2B"):
            core_bytes = 1 if core_str == "1B" else 2
        elif re.fullmatch(r'(0|[1-9][0-9]*)', core_str):
            core_static = int(core_str)
        raw_tp_data = tp.get("TracePointData") or []
        for suffix, hex_id in (tp.get("TracePointIDs") or {}).items():
            if isinstance(raw_tp_data, dict):
                raw_tokens: list[str] = raw_tp_data.get(suffix, [])
            else:
                raw_tokens: list[str] = raw_tp_data
            # Strip (name) suffix for payload layout; keep raw for stringify lookup
            data_tokens = [_parse_data_token(t)[0] for t in raw_tokens]
            payload_size = inst_bytes + core_bytes + compute_payload_size(data_tokens)
            ident = int(hex_id, 16)
            parts = [p for p in [prefix, group, name, tp_type, suffix] if p]
            label = "_".join(parts) if name_underscored else "".join(parts)
            mapping[ident] = {
                "name": label,
                "tp_type": tp_type,
                "inst_size": inst_size,
                "inst_bytes": inst_bytes,
                "core_str": core_str,
                "core_static": core_static,
                "core_bytes": core_bytes,
                "data_tokens": data_tokens,
                "raw_tokens": raw_tokens,
                "stringify_rules": stringify_rules,
                "payload_size": payload_size,
            }
    id_bytes = {"1B": 1, "2B": 2, "3B": 3, "4B": 4}[gen.get("TracePointIDSize", "2B")]
    sync_id = (1 << (8 * id_bytes)) - 1
    mapping[sync_id] = {
        "name": "EST_ID_SYNC",
        "tp_type": "sync",
        "inst_size": "",
        "inst_bytes": 0,
        "data_tokens": [],
        "raw_tokens": [],
        "stringify_rules": {},
        "payload_size": 0,
    }
    return mapping


def find_block(blob: bytes, short_hash: int) -> tuple[int, str]:
    inv = (~short_hash) & 0xFFFFFFFF
    hits: list[tuple[int, str]] = []
    for endian in ("little", "big"):
        pat = short_hash.to_bytes(4, endian) + inv.to_bytes(4, endian)
        start = 0
        while True:
            idx = blob.find(pat, start)
            if idx < 0:
                break
            hits.append((idx, endian))
            start = idx + 1
    if not hits:
        raise ValueError(
            "No EST ram-block magic found. Need magic_start + ~magic_start "
            "matching config short_hash in little- or big-endian bytes."
        )
    return hits[0]


def unpack_meta(blob: bytes, off: int, endian: str) -> dict:
    fields = struct.unpack_from(META_FMT[endian], blob, off)
    return {
        "write_idx": fields[0],
        "is_wrapped": fields[1],
        "is_stopped": fields[2],
    }


def decode_uleb128(data: bytes, off: int) -> tuple[int, int]:
    val = 0
    shift = 0
    start = off
    while True:
        if off >= len(data):
            raise ValueError("truncated ULEB128 timestamp")
        byte = data[off]
        off += 1
        val |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return val & 0xFFFFFFFF, (off - start)
        shift += 7
        if shift >= 32:
            raise ValueError("ULEB128 timestamp longer than 32 bits")


def try_parse_record(data: bytes, off: int, id_bytes: int, ts_mode: str, id_info: dict[int, dict], ts_bytes: int = 4) -> tuple[dict | None, int]:
    if off + id_bytes > len(data):
        return None, 0
    ident = int.from_bytes(data[off : off + id_bytes], "little")
    if ident not in id_info:
        return None, 0

    info = id_info[ident]
    cur = off + id_bytes
    if ts_mode in ("ULEB128", "diff dyn"):
        try:
            val, n = decode_uleb128(data, cur)
            cur += n
            raw_ts = val
        except ValueError:
            return None, 0
    elif ts_mode == "fixed":
        if cur + ts_bytes > len(data): return None, 0
        raw_ts = int.from_bytes(data[cur : cur + ts_bytes], "little")
        cur += ts_bytes
    elif ts_mode == "fixed 2B":
        if cur + 2 > len(data): return None, 0
        raw_ts = int.from_bytes(data[cur : cur + 2], "little")
        cur += 2
    elif ts_mode == "fixed 3B":
        if cur + 3 > len(data): return None, 0
        raw_ts = int.from_bytes(data[cur : cur + 3], "little")
        cur += 3
    elif ts_mode == "fixed 4B":
        if cur + 4 > len(data): return None, 0
        raw_ts = int.from_bytes(data[cur : cur + 4], "little")
        cur += 4
    else:
        return None, 0

    payload_size = info["payload_size"]
    if cur + payload_size > len(data):
        return None, 0

    payload = data[cur : cur + payload_size]
    cur += payload_size

    rec = {
        "id": ident,
        "name": info["name"],
        "tp_type": info["tp_type"],
        "inst_size": info.get("inst_size", ""),
        "inst_bytes": info.get("inst_bytes", 0),
        "core_str": info.get("core_str", ""),
        "core_static": info.get("core_static", None),
        "core_bytes": info.get("core_bytes", 0),
        "data_tokens": info["data_tokens"],
        "raw_tokens": info.get("raw_tokens", []),
        "stringify_rules": info.get("stringify_rules", {}),
        "raw_ts": raw_ts,
        "payload": payload,
    }
    return rec, cur - off


def decode_stream(stream: bytes, id_bytes: int, ts_mode: str, id_info: dict[int, dict], ts_cnt_bytes: int = 4, ts_resolution: int = 1) -> list[dict]:
    pos = 0
    events = []

    ts_accum = 0          # used for ULEB128 only (unbounded Python int)
    overflow_count = 0

    # Derive counter overflow range from hardware counter size (2B->16 bits, 3B->24 bits, 4B->32 bits)
    if ts_mode in ("fixed 2B", "fixed 3B", "fixed 4B"):
        ts_bytes = {"fixed 2B": 2, "fixed 3B": 3, "fixed 4B": 4}[ts_mode]
        overflow_range = 1 << (ts_bytes * 8)
    else:
        ts_bytes = ts_cnt_bytes
        overflow_range = 1 << (ts_cnt_bytes * 8)

    while pos < len(stream):
        rec, n = try_parse_record(stream, pos, id_bytes, ts_mode, id_info, ts_bytes)
        if rec is None or n == 0:
            break
        pos += n

        if ts_mode in ("ULEB128", "diff dyn"):
            if rec["name"] == "EST_ID_SYNC":
                ts_accum = ((ts_accum // overflow_range) + 1) * overflow_range
            else:
                ts_accum += rec["raw_ts"]
            stamp = ts_accum
        else:
            # fixed mode (fixed-width counter)
            if rec["name"] == "EST_ID_SYNC":
                # Overflow event: timer wrapped. est_log passes now_ticks=0,
                # so raw_ts == 0. This moment is the start of the next epoch.
                overflow_count += 1
            stamp = overflow_count * overflow_range + rec["raw_ts"]

        inst_bytes = rec.get("inst_bytes", 0)
        core_bytes = rec.get("core_bytes", 0)
        if inst_bytes > 0:
            rec["instance"] = int.from_bytes(rec["payload"][:inst_bytes], "little")
        else:
            rec["instance"] = None
        if core_bytes > 0:
            rec["dynamic_core"] = int.from_bytes(rec["payload"][inst_bytes:inst_bytes + core_bytes], "little")
        else:
            rec["dynamic_core"] = None

        rec["timestamp"] = stamp * ts_resolution
        del rec["raw_ts"]
        events.append(rec)
    return events



def decode_dump(config_path: Path, dump_path: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    short_hash = parse_short_hash(config.get("short_hash", "0"))
    gen = config.get("General", {})
    buf_size = gen.get("TracerBufferSize", 1000)
    buf_type = gen.get("TracerBufferType", "linear")
    id_size_str = gen.get("TracePointIDSize", "2B")
    id_bytes = {"1B": 1, "2B": 2, "3B": 3, "4B": 4}[id_size_str]
    ts_cnt_size = gen.get("TimestampCntSize", "4B")
    ts_cnt_bytes = {"2B": 2, "3B": 3, "4B": 4}.get(ts_cnt_size, 4)
    ts_mode = gen.get("TimestampStorage", "ULEB128")

    blob = dump_path.read_bytes()
    idx, endian = find_block(blob, short_hash)
    meta_off = idx + 8
    if meta_off + META_SIZE > len(blob):
        raise ValueError("dump truncated in metadata")
    meta = unpack_meta(blob, meta_off, endian)

    buf_off = meta_off + META_SIZE
    buf_end = buf_off + buf_size
    pad_len = (4 - (buf_size % 4)) % 4
    end_magic_off = buf_end + pad_len
    if end_magic_off + 8 > len(blob):
        raise ValueError("dump truncated in buffer or trailing magic")

    inv = (~short_hash) & 0xFFFFFFFF
    end_inv = int.from_bytes(blob[end_magic_off : end_magic_off + 4], endian)
    end_mag = int.from_bytes(blob[end_magic_off + 4 : end_magic_off + 8], endian)
    if end_inv != inv or end_mag != short_hash:
        raise ValueError("trailing magic pair mismatch")

    buffer = blob[buf_off:buf_end]
    write_idx = meta["write_idx"]
    if write_idx > buf_size:
        raise ValueError(f"write_idx ({write_idx}) out of range (buffer_size={buf_size})")

    id_info = load_id_info(config)

    # Determine stream based on buffer type and wrap/stop flags
    if buf_type == "circular" and meta["is_wrapped"] != 0:
        raw_stream = buffer[write_idx:] + buffer[:write_idx]
        # Resynchronize: scan forward to first valid record whose following record is also valid
        sync_pos = 0
        ts_bytes = ts_cnt_bytes if ts_mode == "fixed" else {"fixed 2B": 2, "fixed 3B": 3, "fixed 4B": 4}.get(ts_mode, 0)
        while sync_pos < min(len(raw_stream), 128):
            r1, n1 = try_parse_record(raw_stream, sync_pos, id_bytes, ts_mode, id_info, ts_bytes)
            if r1 is not None and n1 > 0:
                next_pos = sync_pos + n1
                if next_pos == len(raw_stream):
                    break
                r2, n2 = try_parse_record(raw_stream, next_pos, id_bytes, ts_mode, id_info, ts_bytes)
                if r2 is not None and n2 > 0:
                    break
            sync_pos += 1
        stream = raw_stream[sync_pos:]
    else:
        stream = buffer[:write_idx]

    ts_resolution = gen.get("TimestampResolutionValue", 1)
    events = decode_stream(stream, id_bytes, ts_mode, id_info, ts_cnt_bytes, ts_resolution)

    return {
        "config": config,
        "short_hash": short_hash,
        "endian": endian,
        "meta": {
            "buffer_size": buf_size,
            "buffer_type": buf_type,
            "write_idx": meta["write_idx"],
            "is_wrapped": bool(meta["is_wrapped"]),
            "is_stopped": bool(meta["is_stopped"]),
            "timestamp_storage": ts_mode,
            "timestamp_cnt_size": ts_cnt_size,
        },
        "events": events,
    }


def build_output_json(result: dict) -> dict:
    """Build the structured output JSON as specified."""
    config = result["config"]
    short_hash = result["short_hash"]
    gen = config.get("General", {})
    id_size_str = gen.get("TracePointIDSize", "2B")
    id_bytes_count = {"1B": 1, "2B": 2, "3B": 3, "4B": 4}[id_size_str]
    id_hex_digits = id_bytes_count * 2

    config_hash = config.get("hash", "")
    short_hash_str = f"0x{short_hash:08X}"
    short_hash_inv = (~short_hash) & 0xFFFFFFFF
    short_hash_inv_str = f"0x{short_hash_inv:08X}"

    tracepoints = []
    for number, ev in enumerate(result["events"], start=1):
        tp_id = ev["id"]
        id_str = f"0x{tp_id:0{id_hex_digits}X}"
        inst_bytes = ev.get("inst_bytes", 0)
        core_bytes = ev.get("core_bytes", 0)
        core_static = ev.get("core_static", None)
        data = unpack_payload(ev["payload"][inst_bytes + core_bytes:], ev["data_tokens"])
        raw_tokens = ev.get("raw_tokens", [])
        stringify_rules = ev.get("stringify_rules", {})
        for di, d_item in enumerate(data):
            if di < len(raw_tokens):
                _, sname = _parse_data_token(raw_tokens[di])
                if sname and stringify_rules:
                    val = d_item.get("Value")
                    if isinstance(val, list):
                        s_list = []
                        has_any = False
                        for idx, v in enumerate(val):
                            s = lookup_stringify(stringify_rules, sname, v, index=idx, array_size=len(val))
                            s_list.append(s)
                            if s is not None:
                                has_any = True
                        if has_any:
                            d_item["Stringify"] = s_list
                    else:
                        s = lookup_stringify(stringify_rules, sname, val, index=None, array_size=None)
                        if s is not None:
                            d_item["Stringify"] = s
        tp_entry = {
            "Timestamp": ev["timestamp"],
            "ID": id_str,
        }
        if ev.get("instance") is not None:
            tp_entry["Instance"] = ev["instance"]
        if ev.get("dynamic_core") is not None:
            tp_entry["Core"] = ev["dynamic_core"]
        elif core_static is not None:
            tp_entry["Core"] = core_static
        tp_entry["Data"] = data
        tracepoints.append(tp_entry)

    return {
        "General": {
            "TimestampUnit": gen.get("TimestampResolutionUnit"),
            "ConfigHash": config_hash,
            "ShortHash": short_hash_str,
            "ShortHashInverted": short_hash_inv_str,
        },
        "Tracepoints": tracepoints,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decode an EST ram-block memory dump")
    parser.add_argument("config_json")
    parser.add_argument("dump_bin")
    parser.add_argument("--json", action="store_true", help="print machine-readable raw JSON to stdout")
    parser.add_argument("-o", "--output", metavar="FILE.json",
                        help="write decoded tracing to a structured JSON file")
    args = parser.parse_args(argv)

    try:
        result = decode_dump(Path(args.config_json), Path(args.dump_bin))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Write structured output JSON if requested
    if args.output:
        try:
            output_data = build_output_json(result)
            out_path = Path(args.output)
            out_path.write_text(json.dumps(output_data, indent=2), encoding="utf-8")
            print(f"Decoded trace written to: {out_path}")
        except (OSError, ValueError) as exc:
            print(f"Error writing output file: {exc}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(result, indent=2, default=lambda b: b.hex() if isinstance(b, (bytes, bytearray)) else str(b)))
        return 0

    # Human-readable summary to stdout
    meta = result["meta"]
    print(f"endian:           {result['endian']}")
    print(f"buffer_size:      {meta['buffer_size']}")
    print(f"buffer_type:      {meta['buffer_type']}")
    print(f"write_idx:        {meta['write_idx']}")
    print(f"is_wrapped:       {meta['is_wrapped']}")
    print(f"is_stopped:       {meta['is_stopped']}")
    print(f"ts_storage:       {meta['timestamp_storage']}")
    print(f"ts_cnt_size:      {meta.get('timestamp_cnt_size', '4B')}")
    print(f"events:           {len(result['events'])}")
    for i, ev in enumerate(result["events"]):
        inst_bytes = ev.get("inst_bytes", 0)
        if inst_bytes > 0:
            data = unpack_payload(ev["payload"][inst_bytes:], ev["data_tokens"])
            inst_str = f" inst={ev['instance']}"
        else:
            data = unpack_payload(ev["payload"], ev["data_tokens"])
            inst_str = ""
        data_str = ", ".join(str(d['Value']) for d in data) if data else "-"
        print(f"  [{i + 1:4d}] ts={ev['timestamp']:<10} {ev['name']}{inst_str}  data={data_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
