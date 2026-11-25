#!/usr/bin/env python3
"""
generate_est_cfg_files.py

Generates configuration C header and source files (est_cfg.h / est_cfg.c) 
from a JSON configuration file after verifying its canonical hash.

Replaces: generate_trace_cfg.py
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# =============================================================================
# Type Maps & Configuration Lookup Tables
# =============================================================================

TYPE_MAP = {
    "u8": "uint8_t", "s8": "int8_t",
    "u16": "uint16_t", "s16": "int16_t",
    "u32": "uint32_t", "s32": "int32_t",
    "u64": "uint64_t", "s64": "int64_t",
    "f32": "float", "f64": "double"
}

TYPE_SIZES = {
    "u8": 1, "s8": 1,
    "u16": 2, "s16": 2,
    "u32": 4, "s32": 4,
    "u64": 8, "s64": 8,
    "f32": 4, "f64": 8
}

ID_SIZE_MAP = {"1B": 1, "2B": 2, "3B": 3, "4B": 4}

BUF_TYPE_MAP = {
    "circular": "EST_BF_CIRCULAR",
    "linear":   "EST_BF_LINEAR"
}

TS_STORAGE_MAP = {
    "ULEB128":   (0, "EST_TS_ULEB128"),
    "diff dyn":  (0, "EST_TS_ULEB128"),
    "fixed 2B":  (2, "EST_TS_FULL_2B"),
    "fixed 3B":  (3, "EST_TS_FULL_3B"),
    "fixed 4B":  (4, "EST_TS_FULL_4B")
}

# =============================================================================
# Bitfield ('bN') Handling
# =============================================================================
# A TracePointData token "bN" (1<=N<=64) is a bitfield of N bits. Consecutive
# bitfield tokens are packed (concatenated) into as few whole bytes as
# possible; a non-bitfield token always ends the current run. Packing is
# LSB-first: the first field in a run occupies the low bits, each later field
# is shifted up by the sum of the widths before it, and any leftover bits sit
# unused at the TOP of the container. This is a pure value-level bit
# computation (<<, &, |), so it does not depend on target endianness -- only
# the final memcpy of the finished word into the payload buffer does, exactly
# like every other multi-byte TracePointData type already in this project.

BITFIELD_RE = re.compile(r'^b([1-9]|[1-5][0-9]|6[0-4])$')
ARRAY_RE = re.compile(r'^(u8|s8|u16|s16|u32|s32|u64|s64|f32|f64)a([1-9][0-9]*)$')

CONTAINER_C_TYPE = {1: "uint8_t", 2: "uint16_t", 4: "uint32_t", 8: "uint64_t"}


def parse_bitfield_width(token: str):
    """Returns the bit-width N if token is a valid bitfield token 'bN'
    (1<=N<=64), else None."""
    m = BITFIELD_RE.match(token)
    return int(m.group(1)) if m else None


def parse_array_token(token: str):
    """Returns (base_type, count) if token is an array token '<base>a<count>', else None."""
    m = ARRAY_RE.match(token)
    return (m.group(1), int(m.group(2))) if m else None


def strip_data_token(token: str) -> str:
    """Strip optional '(name)' suffix from data token, e.g. 'u8(state)' -> 'u8'."""
    m = re.match(r'^([^()]+)(?:\([A-Za-z0-9_]+\))?$', str(token).strip())
    return m.group(1) if m else str(token).strip()


def is_valid_data_token(token: str) -> bool:
    base = strip_data_token(token)
    return base in TYPE_MAP or parse_bitfield_width(base) is not None or parse_array_token(base) is not None


def bitfield_param_type(width: int) -> str:
    """Smallest standard unsigned type that can hold one bitfield's own width."""
    if width <= 8:
        return "uint8_t"
    if width <= 16:
        return "uint16_t"
    if width <= 32:
        return "uint32_t"
    return "uint64_t"


def token_c_type(token: str) -> str:
    """C parameter type for a TracePointData token, whether an ordinary type
    (looked up in TYPE_MAP), a bitfield token (sized to its own width),
    or an array token (const pointer to base type)."""
    arr = parse_array_token(token)
    if arr is not None:
        base, _ = arr
        return f"const {TYPE_MAP[base]} *"
    width = parse_bitfield_width(token)
    if width is not None:
        return bitfield_param_type(width)
    return TYPE_MAP[token]


def format_c_param(ctype: str, name: str) -> str:
    sep = "" if ctype.endswith("*") or ctype.endswith(" ") else " "
    return f"{ctype}{sep}{name}"


def pick_container_sizes(total_bits: int):
    """Binary decomposition of ceil(total_bits/8) bytes into powers of two
    from {8,4,2,1}, largest first. This is exactly what avoids ever wasting a
    full spare byte when a single power-of-2 container can't hold the bits
    exactly: e.g. 3 bytes needed -> [2,1] (u16+u8) instead of padding a u32."""
    bytes_needed = (total_bits + 7) // 8
    return [p for p in (8, 4, 2, 1) if bytes_needed & p]


def group_bitfield_run(widths):
    """Greedy left-to-right accumulation over one consecutive run of bitfield
    widths: keep adding fields to the current word while the total stays
    within 64 bits; the moment a field would overflow 64 bits, close the
    current word and start a new one with that field. Returns a list of
    (field_widths_in_this_word, total_bits_in_this_word)."""
    words = []
    current, current_bits = [], 0

    def close_word():
        nonlocal current, current_bits
        if current:
            words.append((list(current), current_bits))
        current, current_bits = [], 0

    for w in widths:
        if current_bits + w > 64:
            close_word()
        current.append(w)
        current_bits += w
    close_word()
    return words


def assign_word_to_containers(widths):
    """Given one closed word's field widths (sum <= 64), picks container byte
    sizes via pick_container_sizes and determines how each field's bits map
    onto those containers. A field that straddles a container boundary is
    split: each container it overlaps gets its own contribution. Returns
    (containers_bytes, per_field_parts), where per_field_parts[i] is a list
    of (container_index, shift_in_container, part_width, field_bit_offset)
    tuples for field i -- field_bit_offset is where this part starts within
    the field's OWN value (0 for a field that doesn't straddle, so a
    non-straddling field still produces exactly one part identical to the
    simple single-container case)."""
    total_bits = sum(widths)
    containers = pick_container_sizes(total_bits)

    container_starts, container_ends = [], []
    cum = 0
    for c in containers:
        container_starts.append(cum)
        cum += c * 8
        container_ends.append(cum)

    ranges, pos = [], 0
    for w in widths:
        ranges.append((pos, pos + w))
        pos += w

    per_field_parts = []
    for (start, end) in ranges:
        parts = []
        for ci in range(len(containers)):
            c_start, c_end = container_starts[ci], container_ends[ci]
            part_start, part_end = max(start, c_start), min(end, c_end)
            if part_start < part_end:
                parts.append((ci, part_start - c_start, part_end - part_start, part_start - start))
        assert sum(p[2] for p in parts) == (end - start)  # every bit placed exactly once
        per_field_parts.append(parts)

    return containers, per_field_parts


def resolve_data_layout(data_types):
    """Walks a full TracePointData token list and returns an ordered list of
    payload chunks describing exactly how the packed payload buffer is laid
    out -- either:
      {"kind": "ordinary", "token": t, "arg_index": i}
    or
      {"kind": "bitfield_word", "tokens": [...], "widths": [...],
       "arg_indices": [...], "containers_bytes": [...],
       "per_field_parts": [[(container_idx, shift, part_width, field_bit_offset), ...], ...]}
    A non-bitfield token always closes whatever bitfield run is in progress;
    adjacency (not overall trace-point-wide optimal packing) is what defines
    a run, matching how the existing memcpy-per-token behavior already treats
    each ordinary token as independent."""
    chunks = []
    run_tokens, run_widths, run_indices = [], [], []

    def flush_run():
        if not run_tokens:
            return
        pos = 0
        for fields, _total_bits in group_bitfield_run(run_widths):
            n = len(fields)
            toks = run_tokens[pos:pos + n]
            idxs = run_indices[pos:pos + n]
            containers, per_field_parts = assign_word_to_containers(fields)
            chunks.append({
                "kind": "bitfield_word",
                "tokens": toks,
                "widths": fields,
                "arg_indices": idxs,
                "containers_bytes": containers,
                "per_field_parts": per_field_parts,
            })
            pos += n
        run_tokens.clear()
        run_widths.clear()
        run_indices.clear()

    for i, t in enumerate(data_types):
        w = parse_bitfield_width(t)
        if w is not None:
            run_tokens.append(t)
            run_widths.append(w)
            run_indices.append(i)
        else:
            flush_run()
            arr = parse_array_token(t)
            if arr is not None:
                base, count = arr
                chunks.append({"kind": "array", "token": t, "base_type": base, "count": count, "arg_index": i})
            else:
                chunks.append({"kind": "ordinary", "token": t, "arg_index": i})
    flush_run()
    return chunks


def layout_total_bytes(chunks) -> int:
    total = 0
    for c in chunks:
        if c["kind"] == "ordinary":
            total += TYPE_SIZES[c["token"]]
        elif c["kind"] == "array":
            total += TYPE_SIZES[c["base_type"]] * c["count"]
        else:
            total += sum(c["containers_bytes"])
    return total


# =============================================================================
# Hashing & Validation Utilities
# =============================================================================

def canonical_hash(data: dict, exclude_keys: tuple = ("hash", "schema_hash", "short_hash")) -> str:
    """Calculates canonical SHA-256 hash ignoring key ordering, whitespace, and excluded keys."""
    clean_data = {k: v for k, v in data.items() if k not in exclude_keys}
    canonical_str = json.dumps(clean_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load JSON file '{path}': {e}")
        sys.exit(1)


def verify_config_hash(config: dict) -> str:
    """Verifies that the hash in the JSON config matches its computed canonical hash."""
    calc_hash = canonical_hash(config)
    stored_hash = config.get("hash")

    if not stored_hash:
        print("ERROR: Configuration file is missing the required 'hash' key!")
        sys.exit(1)

    if stored_hash != calc_hash:
        print(
            f"ERROR: Configuration 'hash' mismatch!\n"
            f"  Stored    : {stored_hash}\n"
            f"  Calculated: {calc_hash}"
        )
        sys.exit(1)

    return calc_hash


def verify_short_hash(config: dict, verified_full_hash: str) -> int:
    """Verifies that the config's own short_hash is exactly the 32-bit XOR-fold of its
    (already-verified) full hash -- this generator reads short_hash rather than ever
    recomputing it from scratch for use, but it does not blindly trust it either."""
    stored_short_hash = config.get("short_hash")
    calc_short_hash = compute_32bit_hash(verified_full_hash)

    if not stored_short_hash:
        print("ERROR: Configuration file is missing the required 'short_hash' key!")
        sys.exit(1)

    try:
        stored_value = int(stored_short_hash, 16)
    except (TypeError, ValueError):
        print(f"ERROR: Configuration 'short_hash' value '{stored_short_hash}' is not a valid hex string.")
        sys.exit(1)

    if stored_value != calc_short_hash:
        print(
            f"ERROR: Configuration 'short_hash' mismatch!\n"
            f"  Stored    : {stored_short_hash}\n"
            f"  Calculated: 0x{calc_short_hash:08X}\n"
            f"  (short_hash must be the 32-bit XOR-fold of hash -- manage_est_json_io.py "
            f"computes this automatically when exporting from a spreadsheet or validating.)"
        )
        sys.exit(1)

    return calc_short_hash


# =============================================================================
# Domain Logic Functions
# =============================================================================

def compute_32bit_hash(sha256_hex):
    raw_bytes = bytes.fromhex(sha256_hex)
    hash32 = 0
    for i in range(0, len(raw_bytes), 4):
        chunk = int.from_bytes(raw_bytes[i:i+4], byteorder='big')
        hash32 ^= chunk
    return hash32


def parse_timestamp_api(ts_api_str):
    api = ts_api_str.strip().rstrip(';')

    if re.search(r'&\s*\)', api):
        api_fixed = re.sub(r'&\s*\)', '&timestamp)', api)
        return ["uint32_t timestamp;", f"{api_fixed};"], "timestamp"

    assign_match = re.search(r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.+)$', api)
    if assign_match:
        var_name = assign_match.group(1)
        func_call = assign_match.group(2)
        return [f"uint32_t {var_name} = {func_call};"], var_name

    ref_match = re.search(r'&\s*([a-zA-Z_][a-zA-Z0-9_]*)', api)
    if ref_match:
        var_name = ref_match.group(1)
        return [f"uint32_t {var_name};", f"{api};"], var_name

    return [f"uint32_t timestamp = {api};"], "timestamp"


def format_do_while_macro(macro_name, arg_list_str, statements):
    """Renders an enabled trace point's macro as one statement per line
    instead of one long line, for easier visual inspection (and room to grow,
    e.g. if dynamic conditions get added later). Each statement is indented
    two spaces past the column where 'do {' opens -- which differs per macro
    since it depends on that macro's own name/argument length -- and the
    closing '} while (...)' lines back up under that same brace. Spaces only,
    no tabs, so it renders identically in any editor."""
    header = f"#define {macro_name}({arg_list_str}) do " + "{"
    brace_col = header.rindex("{")
    indent = " " * (brace_col + 2)
    closing_indent = " " * brace_col

    lines = [header + " \\"]
    for stmt in statements:
        lines.append(f"{indent}{stmt} \\")
    lines.append(f"{closing_indent}}} while ((void)0, 0)")
    return "\n".join(lines)


def parse_overflow_api(api_str, default_prefix):
    if not api_str:
        return f"{default_prefix}TimerOverflowHandler"
    cleaned = api_str.strip().rstrip(';')
    match = re.search(r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)', cleaned)
    return match.group(1) if match else f"{default_prefix}TimerOverflowHandler"


def format_typed_macro_val(val, num_bytes):
    hex_str = f"0x{{:0{num_bytes*2}X}}u".format(val)
    if num_bytes == 1:
        return f"((uint8_t){hex_str})"
    elif num_bytes == 2:
        return f"((uint16_t){hex_str})"
    else:
        return f"({hex_str})"


def format_byte_size_val(num_bytes):
    """Plain unsigned literal, deliberately uncast. EST_TRACE_ID_BYTES (which
    reuses this) is compared inside #if directives in est.c, and the
    preprocessor cannot evaluate a C-style cast such as "((uint8_t)1u)" --
    the type name is not a macro, so it is replaced with 0, breaking the
    expression. A plain literal still converts safely and implicitly
    wherever this value is used in ordinary C code."""
    return f"({num_bytes}u)"


def _helper_param_names(n: int, roles: list) -> list[str]:
    """Return parameter names for an est_pack_* helper with n positional arguments.

    roles is a list of sets, one set per position.  Each set contains zero or
    more of the role letters 'i' (instance), 'c' (dynamic core), 'd' (data),
    reflecting every tracepoint that shares this helper.

    Name for position k:
      - Concatenate present roles in order i -> c -> d.
      - If 'd' is among the roles, append the 1-based data index for that
        position (= number of positions 0..k that carry 'd').
      - If only non-data roles ('i', 'c', or 'ic'), no numeric suffix.

    Examples:
      {'i'}       -> "i"
      {'c'}       -> "c"
      {'d'}       -> "d1" / "d2" / ...
      {'i','d'}   -> "id1" / "id2" / ...
      {'c','d'}   -> "cd1" / "cd2" / ...
      {'i','c'}   -> "ic"
      {'i','c','d'} -> "icd1" / "icd2" / ...
    """
    names: list[str] = []
    data_idx = 0
    for k in range(n):
        role_set = roles[k] if k < len(roles) else {'d'}
        if 'd' in role_set:
            data_idx += 1
        prefix = "".join(r for r in ('i', 'c', 'd') if r in role_set)
        if 'd' in role_set:
            names.append(f"{prefix}{data_idx}")
        else:
            names.append(prefix)
    return names


# =============================================================================
# Source Code Generation Engine
# =============================================================================

def generate_configuration_source_code(cfg: dict, config_path: str, short_hash_value: int) -> None:
    output_dir = Path(config_path).parent

    gen = cfg["General"]
    prefix = gen.get("TracerPrefix", "TRC_")
    enabled = gen.get("TracerEnabled", True)
    hash32 = short_hash_value
    hash_hex = f"0x{hash32:08X}uL"

    id_size_str = gen.get("TracePointIDSize", "2B")
    if id_size_str not in ID_SIZE_MAP:
        print(f"Error: Invalid TracePointIDSize '{id_size_str}'. Allowed: 1B, 2B, 3B, 4B.")
        sys.exit(1)
    id_bytes = ID_SIZE_MAP[id_size_str]
    reserved_sync_id = (1 << (id_bytes * 8)) - 1

    buf_type_str = gen.get("TracerBufferType", "circular")
    if buf_type_str not in BUF_TYPE_MAP:
        print(f"Error: Invalid TracerBufferType '{buf_type_str}'. Allowed: 'circular', 'linear'.")
        sys.exit(1)
    buf_type_macro = BUF_TYPE_MAP[buf_type_str]

    ts_cnt_size_str = gen.get("TimestampCntSize", "4B")
    if ts_cnt_size_str not in ("2B", "3B", "4B"):
        print(f"Error: Invalid TimestampCntSize '{ts_cnt_size_str}'. Allowed: '2B', '3B', '4B'.")
        sys.exit(1)
    ts_cnt_bytes = {"2B": 2, "3B": 3, "4B": 4}[ts_cnt_size_str]
    ts_cnt_mask = {2: "((uint32_t)0xFFFFu)", 3: "((uint32_t)0xFFFFFFu)", 4: "((uint32_t)0xFFFFFFFFu)"}[ts_cnt_bytes]

    ts_storage_str = gen.get("TimestampStorage", "ULEB128")
    if ts_storage_str in ("ULEB128", "diff dyn"):
        if buf_type_str == "circular":
            print("Error: 'ULEB128' timestamp storage is forbidden for circular buffers. Use 'fixed'.")
            sys.exit(1)
        ts_bytes = 0
        ts_mode_macro = "EST_TS_ULEB128"
    elif ts_storage_str == "fixed":
        ts_bytes = ts_cnt_bytes
        ts_mode_macro = f"EST_TS_FULL_{ts_cnt_size_str}"
    elif ts_storage_str in ("fixed 2B", "fixed 3B", "fixed 4B"):
        ts_bytes = {"fixed 2B": 2, "fixed 3B": 3, "fixed 4B": 4}[ts_storage_str]
        ts_mode_macro = f"EST_TS_FULL_{ts_bytes}B"
    else:
        print(f"Error: Invalid TimestampStorage '{ts_storage_str}'. Allowed: 'fixed', 'ULEB128'.")
        sys.exit(1)

    overflow_api = parse_overflow_api(gen.get("TimestampOverflowAPI"), prefix)
    ts_code, ts_var = parse_timestamp_api(gen.get("TimestampAPI", "GetTimestamp(&timestamp);"))
    
    requires_atomic = any(tp.get("AtomicSectionRequired", False) for tp in cfg.get("TracePoints", []))

    atomic_start = gen.get("AtomicSectionStartAPI", "").strip().rstrip(';')
    if atomic_start and not atomic_start.endswith(';'): atomic_start += ';'

    atomic_stop = gen.get("AtomicSectionStopAPI", "").strip().rstrip(';')
    if atomic_stop and not atomic_stop.endswith(';'): atomic_stop += ';'

    atomic_header = gen.get("AtomicSectionHeader", "").strip()

    if requires_atomic:
        missing_atomic = []
        if not atomic_start: missing_atomic.append("AtomicSectionStartAPI")
        if not atomic_stop: missing_atomic.append("AtomicSectionStopAPI")
        if not atomic_header: missing_atomic.append("AtomicSectionHeader")
        if missing_atomic:
            print(f"Error: At least one TracePoint has AtomicSectionRequired enabled, "
                  f"but General is missing: {', '.join(missing_atomic)}.")
            sys.exit(1)

    helpers = {}        # key -> effective_types list
    helper_roles = {}   # key -> {"has_inst": bool, "has_data": bool}
    macros = []
    generated_ids = {}

    # Load subtype definitions from the schema's TracePointSubtype property.
    schema_path = Path(config_path).parent / "config_schema.json"
    type_subtypes = {}  # maps type name -> list of subtype suffix strings
    allowed_types = set()
    if schema_path.is_file():
        try:
            with open(schema_path, "r", encoding="utf-8") as sf:
                schema_data = json.load(sf)
            tp_props = (
                schema_data
                .get("properties", {})
                .get("TracePoints", {})
                .get("items", {})
                .get("properties", {})
            )
            tp_sub = tp_props.get("TracePointSubtype") or tp_props.get("TracePointSubtypes", {})
            type_subtypes = tp_sub.get("const", {})
            allowed_types = set(type_subtypes.keys())
            if tp_props.get("TracePointType", {}).get("enum"):
                for t in tp_props["TracePointType"]["enum"]:
                    if t:
                        allowed_types.add(t)
        except Exception as e:
            print(f"Warning: Could not load schema subtypes from '{schema_path}': {e}")
    else:
        print(f"Warning: Schema file '{schema_path}' not found; subtype validation will be skipped.")

    tp_name_unique = cfg.get("General", {}).get("TracePointNameUnique", False)
    name_underscored = cfg.get("General", {}).get("TracePointNameUnderscored", False)
    seen_names: dict[str, int] = {}
    seen_full_names: dict[str, int] = {}

    for idx, tp in enumerate(cfg["TracePoints"]):
        grp = tp.get("TracePointGroup", "")
        name = tp["TracePointName"]
        if tp_name_unique:
            if name in seen_names:
                print(f"Error: Duplicate TracePointName '{name}' detected across trace points (indexes {seen_names[name]} and {idx}). "
                      f"General.TracePointNameUnique is enabled.")
                sys.exit(1)
            seen_names[name] = idx
        tp_type = tp.get("TracePointType", "")
        tp_enabled = tp["TracePointEnabled"] and enabled
        tp_atomic = tp.get("AtomicSectionRequired", False)
        inst_size_str = tp.get("TracePointInstanceSize") or ""
        if inst_size_str and inst_size_str not in ("1B", "2B"):
            print(f"Error: TracePoint '{name}' has invalid TracePointInstanceSize '{inst_size_str}'. Allowed: '1B', '2B', or '' (none).")
            sys.exit(1)
        inst_token = "u8" if inst_size_str == "1B" else ("u16" if inst_size_str == "2B" else None)

        core_str = tp.get("TracePointCore") or ""
        core_static: int | None = None   # int when statically assigned
        core_token: str | None = None    # "u8" or "u16" when dynamic
        if core_str:
            if core_str in ("1B", "2B"):
                core_token = "u8" if core_str == "1B" else "u16"
            elif re.fullmatch(r'(0|[1-9][0-9]*)', core_str):
                core_static = int(core_str)
            else:
                print(f"Error: TracePoint '{name}' has invalid TracePointCore '{core_str}'. "
                      "Allowed: '1B', '2B', a non-negative decimal integer (static), or '' (none).")
                sys.exit(1)

        # Resolve subtypes: typed tracepoints get their suffix list from the schema;
        # tracepoints with no (or absent) TracePointType are single-point (no suffix, key "").
        if tp_type:
            if allowed_types and tp_type not in allowed_types:
                print(f"Error: TracePoint '{name}' has unrecognized TracePointType '{tp_type}'.")
                print(f"  Allowed types defined in schema: {sorted(list(allowed_types))}")
                sys.exit(1)
            suffixes = type_subtypes.get(tp_type, [""])
            if not suffixes:
                suffixes = [""]
        else:
            suffixes = [""]

        raw_tp_data = tp.get("TracePointData", [])
        tokens_to_validate = []
        if isinstance(raw_tp_data, dict):
            for s_tokens in raw_tp_data.values():
                if isinstance(s_tokens, list):
                    tokens_to_validate.extend(s_tokens)
        elif isinstance(raw_tp_data, list):
            tokens_to_validate.extend(raw_tp_data)

        if tokens_to_validate:
            unknown = [dt for dt in tokens_to_validate if not is_valid_data_token(dt)]
            if unknown:
                print(f"Error: TracePoint '{name}' has unrecognized TracePointData type(s): {', '.join(unknown)}.")
                print(f"  Allowed: {', '.join(sorted(TYPE_MAP.keys()))}, a bitfield token 'b1' through 'b64', or an array token '<type>a<size>' (e.g. 'u8a5').")
                sys.exit(1)

        # --- Read (never calculate) this trace point's IDs from the config: one
        # entry per suffix it generates, keyed by suffix name ("" for event). ---
        tp_ids_by_suffix = tp.get("TracePointIDs", {})
        missing = [s for s in suffixes if s not in tp_ids_by_suffix]
        extra = [s for s in tp_ids_by_suffix if s not in suffixes]
        if missing or extra:
            print(f"Error: TracePoint '{name}' (type '{tp_type}') needs exactly one "
                  f"TracePointIDs entry per suffix {[s if s else '(none)' for s in suffixes]}.")
            if missing:
                print(f"  Missing: {[s if s else '(none)' for s in missing]}")
            if extra:
                print(f"  Unexpected: {[s if s else '(none)' for s in extra]}")
            sys.exit(1)

        formatted_id_by_suffix = {}
        for suff in suffixes:
            id_str = tp_ids_by_suffix[suff]
            parts = [p for p in [prefix, grp, name, tp_type, suff] if p]
            macro_name = "_".join(parts) if name_underscored else "".join(parts)
            if macro_name in seen_full_names:
                print(f"Error: Duplicate macro name '{macro_name}' detected. "
                      f"Both trace points (indexes {seen_full_names[macro_name]} and {idx}) generate identical macro name.")
                sys.exit(1)
            seen_full_names[macro_name] = idx

            try:
                tp_id = int(id_str, 16)
            except ValueError:
                print(f"Error: TracePoint '{macro_name}' has an invalid TracePointIDs value "
                      f"'{id_str}' -- must be a hex number such as '0x1A2B'.")
                sys.exit(1)

            if tp_id > reserved_sync_id:
                print(f"Error: TracePoint '{macro_name}' TracePointIDs value {id_str} does not fit "
                      f"in the configured TracePointIDSize ({id_bytes} byte{'s' if id_bytes != 1 else ''}); "
                      f"maximum value is 0x{reserved_sync_id:X}.")
                sys.exit(1)

            if tp_id == reserved_sync_id:
                print(f"\nFATAL ERROR: TracePoint '{macro_name}' uses the reserved sync ID "
                      f"0x{tp_id:X} (EST_ID_SYNC).")
                sys.exit(1)

            if tp_id in generated_ids:
                print(f"\nFATAL ERROR: ID Collision Detected (0x{tp_id:X})!")
                print(f"TracePoint '{macro_name}' has the same ID as '{generated_ids[tp_id]}'.")
                sys.exit(1)
            generated_ids[tp_id] = macro_name

            formatted_id_by_suffix[suff] = format_typed_macro_val(tp_id, id_bytes)

        for suff in suffixes:
            if isinstance(raw_tp_data, dict):
                data_types_raw = raw_tp_data.get(suff, [])
            else:
                data_types_raw = raw_tp_data or []
            data_types = [strip_data_token(t) for t in data_types_raw]

            effective_types = (
                ([inst_token] if inst_token else []) +
                ([core_token] if core_token else []) +
                list(data_types)
            )
            total_payload_bytes = 0
            helper_key = None
            if effective_types:
                data_layout = resolve_data_layout(effective_types)
                total_payload_bytes = layout_total_bytes(data_layout)
                if total_payload_bytes > 65535:
                    print(f"Error: TracePoint '{name}' (suffix '{suff}') has a combined payload size of "
                          f"{total_payload_bytes} bytes, exceeding the 65535-byte limit imposed by "
                          f"est_log's uint16_t payload_len parameter.")
                    sys.exit(1)

                helper_key = "_".join(effective_types)
                if helper_key not in helpers:
                    helpers[helper_key] = effective_types
                    helper_roles[helper_key] = [set() for _ in effective_types]
                # Tag each position with the role it serves for this tracepoint
                _k = 0
                if inst_token is not None:
                    helper_roles[helper_key][_k].add('i'); _k += 1
                if core_token is not None:
                    helper_roles[helper_key][_k].add('c'); _k += 1
                for _ in data_types:
                    helper_roles[helper_key][_k].add('d'); _k += 1

            parts = [p for p in [prefix, grp, name, tp_type, suff] if p]
            macro_name = "_".join(parts) if name_underscored else "".join(parts)
            formatted_id_str = formatted_id_by_suffix[suff]

            macro_args = (
                (["i"] if inst_token else []) +
                (["c"] if core_token else []) +
                [f"d{j+1}" for j in range(len(data_types))]
            )
            macro_args_str = ", ".join(macro_args)

            core_comment = f" /* Core {core_static} */" if core_static is not None else ""
            if not tp_enabled:
                if macro_args:
                    macros.append(f"#define {macro_name}(...) do {{}} while ((void)0, 0){core_comment}")
                else:
                    macros.append(f"#define {macro_name}() do {{}} while ((void)0, 0){core_comment}")
            else:
                statements = []
                if tp_atomic and atomic_start:
                    statements.append(atomic_start)
                statements.extend(ts_code)

                if effective_types:
                    helper_func = f"est_pack_{helper_key}"
                    statements.append(f"uint8_t pbuf[{total_payload_bytes}];")
                    pack_args_str = ", ".join(macro_args)
                    statements.append(f"{helper_func}(pbuf, {pack_args_str});")
                    statements.append(f"est_log({formatted_id_str}, {ts_var}, pbuf, (uint16_t)sizeof(pbuf));")
                else:
                    statements.append(f"est_log({formatted_id_str}, {ts_var}, NULL, ((uint16_t)0u));")

                if tp_atomic and atomic_stop:
                    statements.append(atomic_stop)

                macro_def = format_do_while_macro(macro_name, macro_args_str, statements)
                macros.append(macro_def + core_comment)

    buf_size = gen.get('TracerBufferSize', 1000)
    align_bytes = (4 - (buf_size % 4)) % 4

    # --- Write est_cfg.h ---
    h_file = output_dir / "est_cfg.h"
    with open(h_file, "w", encoding="utf-8") as f:
        f.write(f"#ifndef _EST_CFG_H_\n#define _EST_CFG_H_ ({hash_hex})\n\n")
        f.write("#include <stdint.h>\n#include <stdbool.h>\n")
        if gen.get("TimestampHeader"): f.write(f'#include "{gen["TimestampHeader"]}"\n')
        if atomic_header: f.write(f'#include "{atomic_header}"\n')
        f.write("\n#ifdef __cplusplus\nextern \"C\" {\n#endif\n\n")
        
        f.write(f"#define EST_BUFFER_SIZE         ({buf_size}u)\n")
        f.write(f"#define EST_BUFFER_ALIGN_BYTES  ({align_bytes}u)\n")
        f.write("/* 32-bit fold of config.json's real 256-bit SHA-256 hash (see config.json's\n")
        f.write("   own short_hash field, which generate_est_cfg_files.py verifies this matches\n")
        f.write("   before generating) -- not the hash itself, hence 'short'. */\n")
        f.write(f"#define EST_SHORT_CONFIG_HASH   ({hash_hex})\n")
        f.write(f"#define EST_TRACE_ID_BYTES      {format_byte_size_val(id_bytes)}\n")
        f.write(f"#define EST_BUFFER_TYPE         ({buf_type_macro})\n")
        f.write(f"#define EST_TIMESTAMP_STORAGE   ({ts_mode_macro})\n")
        
        ts_bytes_formatted = format_byte_size_val(ts_bytes) if ts_bytes > 0 else "(0u)"
        f.write(f"#define EST_TS_STORAGE_BYTES    {ts_bytes_formatted}\n")
        f.write(f"#define EST_TS_CNT_BYTES        {format_byte_size_val(ts_cnt_bytes)}\n")
        f.write(f"#define EST_TS_CNT_MASK         {ts_cnt_mask}\n")
        
        sync_str = format_typed_macro_val(reserved_sync_id, id_bytes)
        f.write(f"#define EST_ID_SYNC             {sync_str}\n\n")

        init_fn = f"{prefix}_Init" if name_underscored else f"{prefix}Init"

        f.write("/* Public Application Endpoints */\n")
        f.write(f"void {init_fn}(void);\n")
        f.write(f"void {overflow_api}(void);\n\n")

        for key, types in helpers.items():
            arg_names = _helper_param_names(len(types), helper_roles[key])
            args = ", ".join([format_c_param(token_c_type(t), arg_names[k]) for k, t in enumerate(types)])
            f.write(f"void est_pack_{key}(uint8_t *buf, {args});\n")

        f.write("\n/* Declared here (not only in the internal est_priv.h) because the\n")
        f.write("   trace macros below expand to calls to it from application code,\n")
        f.write("   which is not expected to include that internal header. */\n")
        f.write("void est_log(uint32_t id, uint32_t now_ticks, const uint8_t *payload, uint16_t payload_len);\n")

        f.write("\n/* Trace Point Access Macros */\n")
        for m in macros: f.write(m + "\n")
            
        f.write("\n#ifdef __cplusplus\n}\n#endif\n\n#endif /* _EST_CFG_H_ */\n")

    # --- Write est_cfg.c ---
    c_file = output_dir / "est_cfg.c"
    with open(c_file, "w", encoding="utf-8") as f:
        f.write('#include "est.h"\n#include "est_priv.h"\n#include <string.h>\n\n')
        f.write(f"#define _EST_CFG_C_ ({hash_hex})\n")
        f.write("#if _EST_CFG_H_ != _EST_CFG_C_\n#error \"Hash mismatch between est_cfg.h and est_cfg.c\"\n#endif\n\n")

        f.write(f"void {init_fn}(void) {{\n")
        f.write("    est_init();\n}\n\n")

        f.write(f"void {overflow_api}(void) {{\n")
        f.write("    est_sys_timer_overflow();\n}\n\n")

        for key, types in helpers.items():
            arg_names = _helper_param_names(len(types), helper_roles[key])
            args = ", ".join([format_c_param(token_c_type(t), arg_names[k]) for k, t in enumerate(types)])
            f.write(f"void est_pack_{key}(uint8_t *buf, {args}) {{\n")
            f.write("    uint32_t idx = 0;\n")

            layout = resolve_data_layout(types)
            bf_counter = 0
            for chunk in layout:
                if chunk["kind"] == "ordinary":
                    i = chunk["arg_index"]
                    ctype = TYPE_MAP[chunk["token"]]
                    f.write(f"    memcpy(&buf[idx], &{arg_names[i]}, sizeof({ctype}));\n")
                    f.write(f"    idx += sizeof({ctype});\n")
                elif chunk["kind"] == "array":
                    i = chunk["arg_index"]
                    ctype = TYPE_MAP[chunk["base_type"]]
                    count = chunk["count"]
                    f.write(f"    memcpy(&buf[idx], {arg_names[i]}, sizeof({ctype}) * {count}u);\n")
                    f.write(f"    idx += sizeof({ctype}) * {count}u;\n")
                else:
                    containers = chunk["containers_bytes"]
                    per_container = [[] for _ in containers]
                    for (arg_idx, parts) in zip(chunk["arg_indices"], chunk["per_field_parts"]):
                        for (ci, shift, part_width, field_bit_offset) in parts:
                            per_container[ci].append((arg_idx, shift, part_width, field_bit_offset))

                    for ci, size_bytes in enumerate(containers):
                        c_type = CONTAINER_C_TYPE[size_bytes]
                        ll = "LL" if size_bytes == 8 else ""
                        terms = []
                        for (arg_idx, shift, part_width, field_bit_offset) in per_container[ci]:
                            mask_lit = f"0x{(1 << part_width) - 1:X}u{ll}"
                            term = arg_names[arg_idx]
                            if field_bit_offset != 0:
                                term = f"({term} >> {field_bit_offset}u)"
                            term = f"(({c_type})({term}) & {mask_lit})"
                            if shift != 0:
                                term = f"({term} << {shift}u)"
                            terms.append(term)
                        var_name = f"bf{bf_counter}"
                        bf_counter += 1
                        f.write(f"    {c_type} {var_name} = {' | '.join(terms)};\n")
                        f.write(f"    memcpy(&buf[idx], &{var_name}, sizeof({c_type}));\n")
                        f.write(f"    idx += sizeof({c_type});\n")
            f.write("}\n\n")

    print(f"Successfully generated {h_file.name} and {c_file.name} with Hash {hash_hex}.")


# =============================================================================
# CLI Main Argument Parsing
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="generate_est_cfg_files.py",
        description=(
            "EST Configuration C/H Code Generator.\n"
            "Verifies input JSON configuration hash (hash) and generates "
            "corresponding C header (est_cfg.h) and source (est_cfg.c) files."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=True
    )

    parser.add_argument(
        "--generate_configuration_source_code", "-gcsc",
        metavar="CONFIG_JSON",
        type=str,
        required=True,
        help=(
            "Input JSON configuration file path.\n"
            "Checks if config hash matches content. If valid, generates est_cfg.c and est_cfg.h.\n"
            "Reports an error if hash is missing or mismatched. (Required parameter)"
        )
    )

    args = parser.parse_args()

    config_path = args.generate_configuration_source_code
    config = load_json(config_path)

    # 1. Validate hashes first
    full_hash = verify_config_hash(config)
    print(f"Config hash successfully verified for '{config_path}'.")
    short_hash_value = verify_short_hash(config, full_hash)
    print(f"Config short_hash successfully verified for '{config_path}'.")

    # 2. Execute C/H code generation
    generate_configuration_source_code(config, config_path, short_hash_value)


if __name__ == "__main__":
    main()