#!/usr/bin/env python3
"""
manage_est_json_io.py

Unified CLI utility for EST JSON configuration and spreadsheet schema management.
Replaces extract_config.py, gen_trc_tpl.py, and import_config_to_tpl.py.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

import jsonschema
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

# ODS support check
try:
    import pandas as pd
    from odf.opendocument import OpenDocumentSpreadsheet, load as odf_load
    from odf.style import Style, TableCellProperties, TableColumnProperties, TextProperties
    from odf.table import Table, TableCell, TableColumn, TableRow
    from odf.text import P
    HAS_ODF = True
except ImportError:
    HAS_ODF = False


# =============================================================================
# Helper Utilities & Hashing
# =============================================================================

def canonical_hash(data: dict, exclude_keys: tuple = ("hash", "schema_hash", "short_hash")) -> str:
    """Calculates canonical SHA-256 hash ignoring key ordering, whitespace, and excluded keys."""
    clean_data = {k: v for k, v in data.items() if k not in exclude_keys}
    canonical_str = json.dumps(clean_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def compute_32bit_hash(sha256_hex: str) -> int:
    """Folds a 256-bit SHA-256 hex digest down to 32 bits by XOR-ing successive 4-byte
    chunks -- the config's short_hash is this value, computed from its own hash.
    Minted here (not in generate_est_cfg_files.py, which only ever reads short_hash
    back out of the config and independently re-verifies it against this same
    algorithm, never calculates it for use)."""
    raw_bytes = bytes.fromhex(sha256_hex)
    hash32 = 0
    for i in range(0, len(raw_bytes), 4):
        chunk = int.from_bytes(raw_bytes[i:i + 4], byteorder="big")
        hash32 ^= chunk
    return hash32


def format_short_hash(hash32: int) -> str:
    return f"0x{hash32:08X}"


# Trace point IDs are minted here, not in generate_est_cfg_files.py -- that script
# only ever reads TracePointIDs back out of the config. Kept in sync with the
# TracePointIDSize enum and TracePointType suffix scheme used throughout the project.
ID_SIZE_MAP = {"1B": 1, "2B": 2, "3B": 3, "4B": 4}

VALID_BASE_TYPES = {"u8", "s8", "u16", "s16", "u32", "s32", "u64", "s64", "f32", "f64"}
BITFIELD_RE = re.compile(r'^b([1-9]|[1-5][0-9]|6[0-4])$')
ARRAY_RE = re.compile(r'^(u8|s8|u16|s16|u32|s32|u64|s64|f32|f64)a([1-9][0-9]*)$')
# Token with optional stringify name suffix: u8(name), f32a5(name), b3 (no suffix allowed on bitfields)
DATA_TOKEN_RE = re.compile(r'^((?:u8|s8|u16|s16|u32|s32|u64|s64|f32|f64)(?:a[1-9][0-9]*)?|b(?:[1-9]|[1-5][0-9]|6[0-4]))(\([A-Za-z0-9_]+\))?$')
# Stringify name field pattern: plain name or name[index_range]
STRINGIFY_NAME_RE = re.compile(
    r'^([A-Za-z0-9_]+)(?:\[(\.\.|[0-9]+|[0-9]+\.\.(?:[0-9]+)?|\.\.[0-9]+)\])?$'
)
# Number patterns for Values field
_NUM_PAT = r'[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)'
VALUES_FIELD_RE = re.compile(
    r'^(?:\.\.|' + _NUM_PAT + r'(?:\.\.(?:' + _NUM_PAT + r')?)?' + r'|\.\.' + _NUM_PAT + r')$'
)
# Allowed printf value specifiers (single, not %s and not %i which is for index)
_PRINTF_VAL_SPEC = re.compile(r'%[diouxXeEfgGaA]')
_PRINTF_IDX_SPEC = re.compile(r'%i')
_PRINTF_ANY_SPEC = re.compile(r'%(?:[^%])')


def parse_array_token(token: str) -> tuple[str, int] | None:
    m = ARRAY_RE.match(token)
    if m:
        return m.group(1), int(m.group(2))
    return None


def parse_data_token(raw_tok: str) -> tuple[str, str | None]:
    """Split a data token into (base_token, stringify_name_or_None).

    Examples:
        'u8'           -> ('u8', None)
        'f32a5(foo)'   -> ('f32a5', 'foo')
        'b3'           -> ('b3', None)
    """
    m = DATA_TOKEN_RE.match(str(raw_tok).strip())
    if not m:
        return str(raw_tok).strip(), None
    base = m.group(1)
    suffix = m.group(2)  # '(name)' or None
    name = suffix[1:-1] if suffix else None  # strip parens
    return base, name


def validate_data_token(token: str) -> tuple[bool, str]:
    """Validate a data token string, stripping any trailing (stringify_name) suffix first."""
    token_raw = str(token).strip()
    # Strip optional stringify name suffix before type validation
    base_tok, _sname = parse_data_token(token_raw)
    token_str = base_tok

    if not token_str:
        return False, "Data type token cannot be empty."

    if "a" in token_str:
        base, _, count_str = token_str.partition("a")
        if base.startswith("b"):
            return False, (
                f"Invalid data type '{token_str}': bitfields cannot be stored as arrays. "
                f"Only 'u', 's', and 'f' data types can be stored as arrays."
            )
        if base not in VALID_BASE_TYPES:
            return False, (
                f"Invalid array base type '{base}' in '{token_str}'. "
                f"Allowed base types: {', '.join(sorted(VALID_BASE_TYPES))}."
            )
        if count_str == "":
            return False, f"Invalid array definition '{token_str}': missing array size after 'a' (e.g. '{base}a5')."
        if not count_str.isdigit():
            return False, f"Invalid array definition '{token_str}': array size '{count_str}' must be a number."
        if count_str == "0" or int(count_str) == 0:
            return False, f"Invalid array definition '{token_str}': array size cannot be 0."
        if count_str.startswith("0"):
            return False, f"Invalid array definition '{token_str}': array size '{count_str}' cannot have leading zeros."
        return True, ""

    if token_str in VALID_BASE_TYPES:
        return True, ""

    if BITFIELD_RE.match(token_str):
        return True, ""

    return False, (
        f"Unrecognized data type '{token_raw}'. "
        f"Allowed: {', '.join(sorted(VALID_BASE_TYPES))}, bitfield 'b1'-'b64', "
        f"or array '<type>a<size>' (e.g. 'u8a5'), optionally with stringify reference '(name)'."
    )


# =============================================================================
# TracePointDataStringify Helpers
# =============================================================================

def _parse_num(s: str) -> float:
    """Parse a single number string (decimal, hex 0x, float, E-notation) to float."""
    s = s.strip()
    if s.lower().startswith('0x'):
        return float(int(s, 16))
    return float(s)


def parse_values_field(values_str: str) -> tuple[float, float] | None:
    """Parse a 'Values' field string into a (low, high) float range (inclusive).

    Returns None if the string is invalid.
    Ranges:
        '..'     -> (-inf, +inf)
        'A..'    -> (A, +inf)
        '..B'    -> (-inf, B)
        'A..B'   -> (A, B)  -- A must be <= B
        'A'      -> (A, A)
    """
    import math
    s = values_str.strip()
    if not s:
        return None
    if '..' in s:
        parts = s.split('..', 1)
        lo_str, hi_str = parts[0].strip(), parts[1].strip()
        try:
            lo = _parse_num(lo_str) if lo_str else -math.inf
            hi = _parse_num(hi_str) if hi_str else math.inf
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


def parse_stringify_name_field(name_str: str) -> tuple[str, str | None] | None:
    """Parse a 'Name' field from TracePointDataStringify into (base_name, index_range_or_None).

    Returns None if the format is invalid.
    Examples:
        'state_str'    -> ('state_str', None)        # scalar-only
        'arr_str[..]'  -> ('arr_str', '..')          # all elements
        'arr_str[3]'   -> ('arr_str', '3')           # only index 3
        'arr_str[3..7]'-> ('arr_str', '3..7')        # indexes 3-7 inclusive
        'arr_str[..5]' -> ('arr_str', '..5')         # indexes 0-5 inclusive
        'arr_str[5..]' -> ('arr_str', '5..')         # indexes 5+ inclusive
    """
    m = STRINGIFY_NAME_RE.match(str(name_str).strip())
    if not m:
        return None
    return m.group(1), m.group(2)  # (base_name, index_range or None)


def _index_range_to_bounds(idx_range: str | None, array_size: int | None) -> tuple[int, int]:
    """Convert an index range string to (first_idx, last_idx) inclusive integers.

    array_size is used to resolve open upper bounds; if None, uses a large sentinel.
    """
    LARGE = 2**31 - 1
    size = array_size if array_size is not None else LARGE
    if idx_range is None:
        return 0, 0  # scalar: only index 0 (but never actually used for arrays)
    if idx_range == '..':
        return 0, max(0, size - 1)
    if '..' in idx_range:
        parts = idx_range.split('..', 1)
        lo_s, hi_s = parts[0].strip(), parts[1].strip()
        lo = int(lo_s) if lo_s else 0
        hi = int(hi_s) if hi_s else max(0, size - 1)
        return lo, hi
    # single index
    v = int(idx_range)
    return v, v


def validate_stringify_format(fmt: str) -> tuple[bool, str]:
    """Validate that a Stringify format string has at most one value specifier,
    no %s, and that all % sequences are valid.

    Returns (is_ok, error_message).
    """
    # Find all % sequences (not %%)
    i = 0
    value_spec_count = 0
    index_spec_count = 0
    while i < len(fmt):
        if fmt[i] == '%':
            if i + 1 >= len(fmt):
                return False, f"Trailing '%' at end of format string: {fmt!r}"
            nxt = fmt[i + 1]
            if nxt == '%':
                i += 2  # escaped literal %
                continue
            if nxt == 'i':
                index_spec_count += 1
                i += 2
                continue
            if nxt == 's':
                return False, f"%%s is not allowed in Stringify format (strings not supported): {fmt!r}"
            # Check for standard C printf specifier: optional flags/width/precision + conversion
            # Accept a simplified form: optional flags [-+ #0], optional width [0-9]+,
            # optional .precision [.][0-9]*, then conversion letter
            j = i + 1
            # skip optional flags
            while j < len(fmt) and fmt[j] in '-+ #0':
                j += 1
            # skip optional width
            while j < len(fmt) and fmt[j].isdigit():
                j += 1
            # skip optional .precision
            if j < len(fmt) and fmt[j] == '.':
                j += 1
                while j < len(fmt) and fmt[j].isdigit():
                    j += 1
            if j >= len(fmt):
                return False, f"Incomplete printf specifier in: {fmt!r}"
            conv = fmt[j]
            if conv not in 'diouxXeEfgGaA':
                return False, f"Invalid printf conversion character '%{conv}' in: {fmt!r}"
            value_spec_count += 1
            i = j + 1
        else:
            i += 1
    if value_spec_count > 1:
        return False, f"More than one value specifier in Stringify format: {fmt!r}"
    return True, ""


def _ranges_overlap(r1: tuple[float, float], r2: tuple[float, float]) -> bool:
    """Return True if two inclusive float ranges overlap."""
    return max(r1[0], r2[0]) <= min(r1[1], r2[1])


def validate_stringify_table(stringify_rows: list[dict], error_prefix: str = "VALIDATION FAILED") -> None:
    """Validate the entire TracePointDataStringify table.

    Checks:
    1. Each Name field has valid format (plain or with [index_range]).
    2. Each Values field is a valid number or range, and range is not inverted.
    3. Each Stringify format string has valid printf specifiers (no %s, <=1 value spec).
    4. Within the same effective name (base_name + index_range), no value ranges overlap.

    Aborts with sys.exit(1) on any failure.
    """
    import math
    # Group entries by (base_name, idx_range_str)
    groups: dict[tuple[str, str | None], list[tuple[int, tuple[float, float]]]] = {}

    for row_idx, row in enumerate(stringify_rows, start=1):
        name_raw = str(row.get("Name", "")).strip()
        values_raw = str(row.get("Values", "")).strip()
        fmt_raw = str(row.get("Stringify", "")).strip()

        # 1. Validate Name
        parsed_name = parse_stringify_name_field(name_raw)
        if parsed_name is None:
            print(f"\n{error_prefix} in TracePointDataStringify:\n"
                  f"  Row {row_idx}: Invalid Name '{name_raw}'. "
                  f"Allowed: plain name (e.g. 'state_str') or name with index range (e.g. 'arr_str[..]').")
            sys.exit(1)
        base_name, idx_range = parsed_name

        # 2. Validate Values
        val_range = parse_values_field(values_raw)
        if val_range is None:
            print(f"\n{error_prefix} in TracePointDataStringify:\n"
                  f"  Row {row_idx}: Invalid Values '{values_raw}'. "
                  f"Allowed: single number, '..' (all), 'A..', '..B', or 'A..B' where A<=B. "
                  f"Numbers may be decimal, hex (0x), float (dot separator), or E-notation.")
            sys.exit(1)

        # 3. Validate Stringify format
        fmt_ok, fmt_err = validate_stringify_format(fmt_raw)
        if not fmt_ok:
            print(f"\n{error_prefix} in TracePointDataStringify:\n"
                  f"  Row {row_idx}: Invalid Stringify format: {fmt_err}")
            sys.exit(1)

        # 4. Collect for overlap check
        key = (base_name, idx_range)
        if key not in groups:
            groups[key] = []
        groups[key].append((row_idx, val_range))

    # Overlap detection within each group
    for (base_name, idx_range), entries in groups.items():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                r1_idx, r1 = entries[i]
                r2_idx, r2 = entries[j]
                if _ranges_overlap(r1, r2):
                    key_label = base_name if idx_range is None else f"{base_name}[{idx_range}]"
                    print(f"\n{error_prefix} in TracePointDataStringify:\n"
                          f"  Overlapping value ranges for Name '{key_label}':\n"
                          f"  Row {r1_idx}: ({r1[0]}, {r1[1]})\n"
                          f"  Row {r2_idx}: ({r2[0]}, {r2[1]})\n"
                          f"  Value ranges for the same Name (and index range) must be disjoint.")
                    sys.exit(1)


def validate_stringify_references(tracepoints: list[dict], overrides: list[dict],
                                  stringify_rows: list[dict],
                                  error_prefix: str = "VALIDATION FAILED") -> None:
    """Check that every stringify name referenced in TracePointData / TracePointDataOverride
    exists in TracePointDataStringify, and that the indexed vs non-indexed form matches
    whether the data token is an array or scalar.

    Emits warnings for form mismatches (but does not abort).
    Aborts if a name is undefined.
    """
    # Build set of defined base names (ignoring index range — a name can have multiple rows
    # with different index ranges, all sharing the same base name)
    defined_names: dict[str, bool] = {}  # base_name -> has_indexed_form
    for row in stringify_rows:
        name_raw = str(row.get("Name", "")).strip()
        parsed = parse_stringify_name_field(name_raw)
        if parsed is None:
            continue
        bname, idx_range = parsed
        if bname not in defined_names:
            defined_names[bname] = (idx_range is not None)
        else:
            if idx_range is not None:
                defined_names[bname] = True

    def _check_tokens(tokens: list[str], context: str) -> None:
        for tok in tokens:
            base_tok, sname = parse_data_token(str(tok))
            if sname is None:
                continue
            if sname not in defined_names:
                print(f"\n{error_prefix}: Stringify name '{sname}' referenced in "
                      f"{context} token '{tok}' is not defined in TracePointDataStringify.")
                sys.exit(1)
            # Check array/scalar mismatch
            arr = ARRAY_RE.match(base_tok)
            is_array = arr is not None
            name_is_indexed = defined_names[sname]
            if is_array and not name_is_indexed:
                print(f"WARNING: Stringify name '{sname}' is defined without index ranges "
                      f"(for scalar tokens) but applied to array token '{tok}' in {context}. "
                      f"To apply stringify to array elements, define the name with index ranges "
                      f"in TracePointDataStringify (e.g. Name='{sname}[..]'). "
                      f"Stringify will be ignored for this token.")
            elif not is_array and name_is_indexed:
                print(f"WARNING: Stringify name '{sname}' is defined with index ranges "
                      f"(for array tokens) but applied to scalar token '{tok}' in {context}. "
                      f"To apply stringify to a scalar, define the name without index ranges "
                      f"in TracePointDataStringify (e.g. Name='{sname}'). "
                      f"Stringify will be ignored for this token.")

    for tp in tracepoints:
        tp_name = tp.get("TracePointName", "?")
        data = tp.get("TracePointData", [])
        if isinstance(data, list):
            _check_tokens(data, f"TracePoint '{tp_name}' TracePointData")
        elif isinstance(data, dict):
            for suf, toks in data.items():
                _check_tokens(toks, f"TracePoint '{tp_name}' TracePointData[{suf}]")

    for r_idx, row in enumerate(overrides, start=1):
        toks = row.get("TracePointDataOverride", [])
        if isinstance(toks, list):
            _check_tokens(toks, f"TracePointDataOverrides row {r_idx}")


def match_filter(filter_kind: str, pattern: str, value: str) -> bool:
    """Matches a value against pattern using either 'wildcards' (* and ?) or 'regex'."""
    pat = str(pattern).strip() if pattern is not None else ""
    val = str(value).strip() if value is not None else ""
    if filter_kind == "wildcards":
        escaped = re.escape(pat)
        regex_str = escaped.replace(r"\*", ".*").replace(r"\?", ".")
        try:
            return bool(re.fullmatch(regex_str, val))
        except re.error:
            return False
    elif filter_kind == "regex":
        try:
            return bool(re.fullmatch(pat, val))
        except re.error:
            return False
    return False


def validate_override_row(row: dict, row_idx: int) -> tuple[bool, str]:
    kind = row.get("TracePointFilterKind", "")
    if kind not in ("wildcards", "regex"):
        return False, f"Row {row_idx}: Invalid TracePointFilterKind '{kind}'. Allowed: 'wildcards', 'regex'."

    if kind == "regex":
        for field in ("TracePointGroupFilter", "TracePointNameFilter", "TracePointTypeFilter", "TracePointSubtypeFilter"):
            pat = str(row.get(field, "")).strip()
            try:
                re.compile(pat)
            except re.error as e:
                return False, f"Row {row_idx}: Invalid regular expression in '{field}' ('{pat}'): {e}."

    data_tokens = row.get("TracePointDataOverride", [])
    if not isinstance(data_tokens, list):
        return False, f"Row {row_idx}: 'TracePointDataOverride' must be a list of data tokens."

    for tok in data_tokens:
        is_valid, err = validate_data_token(tok)
        if not is_valid:
            return False, f"Row {row_idx}: {err}"

    return True, ""


def match_and_verify_overrides(overrides: list[dict], tracepoints: list[dict], type_subtypes: dict[str, list[str]], error_prefix: str = "VALIDATION FAILED") -> None:
    for r_idx, row in enumerate(overrides, start=1):
        is_ok, err_msg = validate_override_row(row, r_idx)
        if not is_ok:
            print(f"\n{error_prefix} in TracePointDataOverrides:\n  {err_msg}")
            sys.exit(1)

    all_endpoints = []
    for tp in tracepoints:
        grp = tp.get("TracePointGroup", "")
        name = tp.get("TracePointName", "")
        raw_type = tp.get("TracePointType")
        tp_type = str(raw_type).strip() if raw_type is not None else ""
        suffixes = type_subtypes.get(tp_type, [""]) if tp_type else [""]
        for suff in suffixes:
            all_endpoints.append((tp, grp, name, tp_type, suff))

    # Check each endpoint: at most one unique override data definition allowed
    for ep in all_endpoints:
        _, grp, name, tp_type, suff = ep
        matching_rows = []
        for r_idx, row in enumerate(overrides, start=1):
            f_kind = row.get("TracePointFilterKind", "wildcards")
            grp_f = str(row.get("TracePointGroupFilter", ""))
            name_f = str(row.get("TracePointNameFilter", ""))
            type_f = str(row.get("TracePointTypeFilter", ""))
            sub_f = str(row.get("TracePointSubtypeFilter", ""))
            if (match_filter(f_kind, grp_f, grp) and
                match_filter(f_kind, name_f, name) and
                match_filter(f_kind, type_f, tp_type) and
                match_filter(f_kind, sub_f, suff)):
                matching_rows.append((r_idx, row))

        if len(matching_rows) > 1:
            first_data = list(matching_rows[0][1].get("TracePointDataOverride", []))
            if any(list(r[1].get("TracePointDataOverride", [])) != first_data for r in matching_rows[1:]):
                conflicts_desc = [f"Row {r[0]} ({r[1].get('TracePointDataOverride', [])})" for r in matching_rows]
                print(
                    f"\n{error_prefix}: Conflicting data overrides for trace point '{grp}{name}' "
                    f"(type '{tp_type or '(none)'}', subtype '{suff or '(none)'}')!\n"
                    f"  Matches multiple override rows with differing data: {', '.join(conflicts_desc)}."
                )
                sys.exit(1)

def compute_tracepoint_id(group_name: str, point_name: str, suffix: str, id_bytes: int) -> int:
    """Derives a stable numeric ID from a trace point's group+name+suffix -- each macro
    suffix a trace point generates (Send/Receive, the six task suffixes, etc.) gets its
    own distinct ID. Deterministic: the same group+name+suffix always folds to the same
    ID as long as id_bytes doesn't change."""
    key_str = f"{group_name}{point_name}{suffix}"
    sha256_bytes = hashlib.sha256(key_str.encode("utf-8")).digest()

    tp_id = 0
    for i in range(0, len(sha256_bytes), id_bytes):
        chunk_bytes = sha256_bytes[i:i + id_bytes]
        if len(chunk_bytes) < id_bytes:
            chunk_bytes = chunk_bytes.ljust(id_bytes, b"\x00")
        chunk = int.from_bytes(chunk_bytes, byteorder="big")
        tp_id ^= chunk

    mask = (1 << (id_bytes * 8)) - 1
    return tp_id & mask


def format_tracepoint_id(tp_id: int, id_bytes: int) -> str:
    """Renders an integer ID as the hex string form config.json stores: '0x' followed by
    uppercase digits, zero-padded to the configured byte width."""
    return f"0x{tp_id:0{id_bytes * 2}X}"


def load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading JSON file '{path}': {e}")
        sys.exit(1)


def is_schema_json(path: str) -> bool:
    """Checks if a JSON file is a JSON Schema (by inspecting structure)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if "$schema" in data or "properties" in data:
                return True
    except Exception:
        pass
    return False


def is_config_json(path: str) -> bool:
    """Checks if a JSON file is a Tracer configuration file (not a schema)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # A tracer config has General or TracePoints and is not a meta-schema
            if "$schema" not in data and ("General" in data or "TracePoints" in data):
                return True
    except Exception:
        pass
    return False


def resolve_schema_and_config(path_a: str, path_b: str) -> Tuple[str, str]:
    """Identifies which argument is schema and which is config by inspecting their content,
    regardless of command-line argument order."""
    if is_schema_json(path_a) and is_config_json(path_b):
        return path_a, path_b
    if is_schema_json(path_b) and is_config_json(path_a):
        return path_b, path_a

    # Fallback to file name hints if content check was inconclusive
    if "schema" in Path(path_a).name.lower() and "schema" not in Path(path_b).name.lower():
        return path_a, path_b
    if "schema" in Path(path_b).name.lower() and "schema" not in Path(path_a).name.lower():
        return path_b, path_a

    return path_a, path_b


def save_json(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except PermissionError:
        print(f"ERROR: Cannot write to '{path}': File is locked or currently open in another application.")
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: Cannot write to '{path}': {e}")
        sys.exit(1)


def get_schema_hash(schema: dict) -> Tuple[str, bool]:
    """
    Computes property hash of schema.
    Returns (calculated_hash, is_valid)
    """
    props = schema.get("properties", {})
    calc = canonical_hash(props, exclude_keys=())
    stored = schema.get("hash")
    return calc, (stored == calc)


def verify_schema_has_valid_hash(schema: dict) -> str:
    calc, is_valid = get_schema_hash(schema)
    if not is_valid:
        stored = schema.get("hash")
        if stored is None:
            print("ERROR: Schema has no 'hash' property!")
        else:
            print(f"ERROR: Schema 'hash' mismatch!\n  Stored    : {stored}\n  Calculated: {calc}")
        sys.exit(1)
    return calc


def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


def convert_xlsx_to_ods_soffice(xlsx_path: str, soffice: str) -> None:
    xlsx = Path(xlsx_path)
    cmd = [
        soffice, "--headless",
        "--convert-to", "ods",
        "--outdir", str(xlsx.parent),
        str(xlsx),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print("LibreOffice conversion failed:", e.stderr.decode(errors="replace") if e.stderr else e)


def disable_ods_show_page_breaks(ods_path: str) -> None:
    """Force the document-wide 'ShowPageBreaks' view setting to false in an ODS file.

    When LibreOffice (headless) converts the generated .xlsx into .ods, it leaves
    'ShowPageBreaks' set to true in settings.xml. That single document-wide toggle is
    what makes Calc draw the dashed print-area/page-break boundary in Normal view; it
    only becomes visible on whichever sheet's content happens to trigger an early
    automatic page split (typically the first, narrower info sheet), which is why it
    looks like only the first worksheet is affected even though the setting itself
    applies to the whole document. Forcing it to false removes the artifact everywhere.
    Safe to call on any .ods file, including ones with no settings.xml entry yet
    (e.g. from the direct/odfpy fallback engine), in which case the item is inserted.
    """
    with open(ods_path, 'rb') as f:
        buf = io.BytesIO(f.read())

    zin = zipfile.ZipFile(buf, 'r')
    out_buf = io.BytesIO()
    zout = zipfile.ZipFile(out_buf, 'w', compression=zipfile.ZIP_DEFLATED)

    for item in zin.infolist():
        content = zin.read(item.filename)
        if item.filename == 'settings.xml':
            text = content.decode('utf-8')
            if 'ShowPageBreaks' in text:
                text = re.sub(
                    r'(<config:config-item\s+[^>]*?config:name=["\']ShowPageBreaks["\'][^>]*?>)\s*true\s*(</config:config-item>)',
                    r'\g<1>false\g<2>',
                    text
                )
            else:
                cfg_set_tag = '<config:config-item-set config:name="ooo:configuration-settings">'
                if cfg_set_tag in text:
                    item_xml = '<config:config-item config:name="ShowPageBreaks" config:type="boolean">false</config:config-item>'
                    text = text.replace(cfg_set_tag, cfg_set_tag + item_xml, 1)
            content = text.encode('utf-8')
        zout.writestr(item, content)

    zin.close()
    zout.close()

    try:
        with open(ods_path, 'wb') as f:
            f.write(out_buf.getvalue())
    except PermissionError:
        print(f"ERROR: Cannot write to '{ods_path}': File is locked or currently open in another application.")
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: Cannot write to '{ods_path}': {e}")
        sys.exit(1)


# =============================================================================
# Direct ODS Generation Engine (Fallback without LibreOffice)
# =============================================================================

def create_ods_directly(schema: dict, output_ods_path: str, schema_hash: str, config: dict = None, config_hash: str = None, short_hash: str = None) -> None:
    if not HAS_ODF:
        print("ERROR: Direct ODS creation requires pandas and odfpy (pip install pandas odfpy).")
        sys.exit(1)

    doc = OpenDocumentSpreadsheet()
    root_props = schema.get("properties", {})

    # Direct ODS styling
    h_style = Style(name="Header", family="table-cell")
    h_style.addElement(TextProperties(attributes={"fontweight": "bold", "color": "#000000"}))
    h_style.addElement(TableCellProperties(attributes={"backgroundcolor": "#D9D9D9"}))
    doc.automaticstyles.addElement(h_style)

    b_style = Style(name="Bold", family="table-cell")
    b_style.addElement(TextProperties(attributes={"fontweight": "bold"}))
    doc.automaticstyles.addElement(b_style)

    col_style_map = {}

    def get_col_style(width_cm: float) -> str:
        key = f"{width_cm:.2f}"
        if key not in col_style_map:
            name = f"co_w_{len(col_style_map) + 1}"
            c_style = Style(name=name, family="table-column")
            c_style.addElement(TableColumnProperties(columnwidth=f"{width_cm:.2f}cm"))
            doc.automaticstyles.addElement(c_style)
            col_style_map[key] = name
        return col_style_map[key]

    def append_table(name, rows, col_widths=None):
        table = Table(name=name)
        if col_widths:
            for w in col_widths:
                table.addElement(TableColumn(stylename=get_col_style(w)))
        for row in rows:
            tr = TableRow()
            for is_hdr, is_bold, val in row:
                cell = TableCell(valuetype="string")
                if is_hdr:
                    cell.setAttribute("stylename", "Header")
                elif is_bold:
                    cell.setAttribute("stylename", "Bold")
                cell.addElement(P(text="" if val is None else str(val)))
                tr.addElement(cell)
            table.addElement(tr)
        doc.spreadsheet.addElement(table)

    # ConfigInfo Sheet (if config exists)
    if config_hash:
        cfg_rows = [
            [(True, False, "Key"), (True, False, "Value")],
            [(False, True, "Config hash (SHA-256)"), (False, False, config_hash)],
            [(False, True, "Config short hash (32-bit)"), (False, False, short_hash)],
            [(False, True, "Schema hash (SHA-256)"), (False, False, schema_hash)],
            [(False, True, "Generated at (UTC)"), (False, False, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))]
        ]
        append_table("ConfigInfo", cfg_rows, col_widths=[5.6, 14.0])

    # SchemaInfo Sheet
    schema_rows = [
        [(True, False, "Key"), (True, False, "Value")],
        [(False, True, "Schema title"), (False, False, schema.get("title", ""))],
        [(False, True, "Properties hash (SHA-256)"), (False, False, schema_hash)],
        [(False, True, "Generated at (UTC)"), (False, False, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))]
    ]
    append_table("SchemaInfo", schema_rows, col_widths=[5.6, 14.0])

    def _format_val(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, list):
            return ", ".join(map(str, v))
        if isinstance(v, dict):
            distinct = list({tuple(x) if isinstance(x, list) else x for x in v.values()})
            if len(distinct) == 1:
                x = distinct[0]
                return ", ".join(map(str, x)) if isinstance(x, (list, tuple)) else str(x)
            return ""
        return str(v)

    # Data Sheets
    for p_name, p_schema in root_props.items():
        if p_schema.get("type") == "object":
            rows = [[(True, False, "Property"), (True, False, "Value")]]
            sub_props = p_schema.get("properties", {})
            sec_data = (config.get(p_name) or {}) if config else {}
            for k in sub_props.keys():
                v = _format_val(sec_data.get(k, ""))
                rows.append([(False, True, k), (False, False, v)])
            append_table(p_name, rows, col_widths=[5.6, 8.0])

        elif p_schema.get("type") == "array" and p_schema.get("items", {}).get("type") == "object":
            item_props = p_schema.get("items", {}).get("properties", {})
            headers = [h for h in item_props.keys() if h not in ("TracePointIDs", "TracePointSubtype", "TracePointSubtypes")]
            rows = [[(True, False, h) for h in headers]]
            items = (config.get(p_name) or []) if config else []
            for item in items:
                row = []
                for h in headers:
                    v = _format_val(item.get(h, ""))
                    row.append((False, False, v))
                rows.append(row)
            if not items:
                for _ in range(10):
                    rows.append([(False, False, "") for _ in headers])
            col_widths = [round(max(len(str(h)) + 5, 18) * 0.20, 2) for h in headers]
            append_table(p_name, rows, col_widths=col_widths)

    try:
        doc.save(output_ods_path)
    except PermissionError:
        print(f"ERROR: Cannot write to '{output_ods_path}': File is locked or currently open in another application.")
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: Cannot write to '{output_ods_path}': {e}")
        sys.exit(1)
    print(f"Directly generated ODS spreadsheet: {output_ods_path}")


# =============================================================================
# Enum Extraction Helper
# =============================================================================

def _get_enum_for_dropdown(sub: dict) -> list | None:
    """Extract a dropdown-friendly enum list from a property schema.

    Handles three cases:
    - Plain ``{"enum": [...]}`` — returned as-is.
    - ``{"anyOf": [{"enum": [...]}, ...]}`` — the enum values from the first
      branch are returned, with ``"..."`` appended to signal that the user
      may also type a free-form value (e.g. a numeric core-ID instead of
      the predefined ``"1B"``/``"2B"`` size tokens).
    - All other schemas — returns ``None`` (no dropdown).
    """
    if "enum" in sub:
        return list(sub["enum"])
    any_of = sub.get("anyOf")
    if any_of and isinstance(any_of, list):
        for branch in any_of:
            if "enum" in branch:
                values = list(branch["enum"])
                # Ensure "..." sentinel is present (may already be in schema)
                if "..." not in values:
                    values.append("...")
                return values
    return None


# =============================================================================
# XLSX Creation & Population Engine
# =============================================================================


def _set_sheet_text_format(ws, n_cols: int) -> None:
    """Set number_format='@' (text) for every column in the worksheet.

    This is done at the column-dimension level which applies to the entire column
    in one shot -- without iterating over individual cells -- so any value the
    user types is kept verbatim as text and never auto-converted by the spreadsheet
    application (e.g. '0xFF', '7..', '3.5e-2' are preserved as-is).
    """
    for col_idx in range(1, n_cols + 1):
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].number_format = "@"


def _to_str(val) -> str:
    """Convert any value to a plain string for text-only spreadsheet cells."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, list):
        return ", ".join(_to_str(v) for v in val)
    if isinstance(val, dict):
        distinct = list({tuple(v) if isinstance(v, list) else v for v in val.values()})
        if len(distinct) == 1:
            v = distinct[0]
            return ", ".join(_to_str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
        return ""
    return str(val)


def build_xlsx_spreadsheet(schema: dict, filename: str, schema_hash: str, config: dict = None, config_hash: str = None, short_hash: str = None) -> None:
    root_props = schema.get("properties", {})
    wb = Workbook()
    wb.remove(wb.active)

    header_font = Font(bold=True, color="000000")
    header_fill = PatternFill("solid", fgColor="D9D9D9")
    bold_font = Font(bold=True)
    thin = Border(left=Side(style="thin"), right=Side(style="thin"), top=Side(style="thin"), bottom=Side(style="thin"))

    def style_header(ws, row=1):
        for cell in ws[row]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", wrap_text=False)
            cell.border = thin

    # 1. Optional ConfigInfo
    if config_hash:
        ws_cfg = wb.create_sheet("ConfigInfo")
        ws_cfg.append(["Key", "Value"])
        style_header(ws_cfg)
        _set_sheet_text_format(ws_cfg, 2)
        cfg_rows = [
            ("Config hash (SHA-256)", config_hash),
            ("Config short hash (32-bit)", short_hash),
            ("Schema hash (SHA-256)", schema_hash),
            ("Imported at (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        ]
        for k, v in cfg_rows:
            ws_cfg.append([_to_str(k), _to_str(v)])
        for r in range(2, len(cfg_rows) + 2):
            ws_cfg.cell(row=r, column=1).font = bold_font
        ws_cfg.column_dimensions["A"].width = 28
        ws_cfg.column_dimensions["B"].width = 70

    # 2. SchemaInfo
    ws_info = wb.create_sheet("SchemaInfo")
    ws_info.append(["Key", "Value"])
    style_header(ws_info)
    _set_sheet_text_format(ws_info, 2)
    info_rows = [
        ("Schema title", schema.get("title", "")),
        ("Properties hash (SHA-256)", schema_hash),
        ("Generated at (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    ]
    for k, v in info_rows:
        ws_info.append([_to_str(k), _to_str(v)])
    for r in range(2, len(info_rows) + 2):
        ws_info.cell(row=r, column=1).font = bold_font
    ws_info.column_dimensions["A"].width = 28
    ws_info.column_dimensions["B"].width = 70

    # 3. Data sheets
    for prop_name, prop_schema in root_props.items():
        if prop_schema.get("type") == "object":
            ws = wb.create_sheet(prop_name)
            ws.append(["Property", "Value"])
            style_header(ws)
            _set_sheet_text_format(ws, 2)
            props = prop_schema.get("properties", {})
            sec_data = (config.get(prop_name) or {}) if config else {}

            for row_idx, (k, sub) in enumerate(props.items(), start=2):
                val = sec_data.get(k)
                ws.append([k, _to_str(val)])
                ws.cell(row=row_idx, column=1).font = bold_font

                cell_ref = f"B{row_idx}"
                if sub.get("type") == "boolean":
                    dv = DataValidation(type="list", formula1='"TRUE,FALSE"', allow_blank=True)
                    ws.add_data_validation(dv)
                    dv.add(cell_ref)
                else:
                    enum_vals = _get_enum_for_dropdown(sub)
                    if enum_vals is not None:
                        formula = '"' + ",".join(map(str, enum_vals)) + '"'
                        dv = DataValidation(type="list", formula1=formula, allow_blank=True)
                        ws.add_data_validation(dv)
                        dv.add(cell_ref)

            ws.column_dimensions["A"].width = 28
            ws.column_dimensions["B"].width = 40
            ws.freeze_panes = "A2"

        elif prop_schema.get("type") == "array" and prop_schema.get("items", {}).get("type") == "object":
            item_schema = prop_schema["items"]
            item_props = item_schema.get("properties", {})
            # TracePointIDs is intentionally excluded: it's never loaded into or read
            # from the spreadsheet, since each value only depends on Group/Name/suffix
            # and is always reproducible -- manage_est_json_io.py computes it directly
            # at export time.
            headers = [h for h in item_props.keys() if h not in ("TracePointIDs", "TracePointSubtype", "TracePointSubtypes")]

            ws = wb.create_sheet(prop_name)
            ws.append(headers)
            style_header(ws)
            _set_sheet_text_format(ws, len(headers))

            items = (config.get(prop_name) or []) if config else []
            for item in items:
                row_vals = [_to_str(item.get(h)) for h in headers]
                ws.append(row_vals)

            if not items:
                for _ in range(40):
                    ws.append([""] * len(headers))

            for col_idx, h in enumerate(headers, start=1):
                sub = item_props[h]
                col_letter = get_column_letter(col_idx)
                range_ref = f"{col_letter}2:{col_letter}1000"
                if sub.get("type") == "boolean":
                    dv = DataValidation(type="list", formula1='"TRUE,FALSE"', allow_blank=True)
                    ws.add_data_validation(dv)
                    dv.add(range_ref)
                elif h == "TracePointType":
                    tp_sub_prop = item_props.get("TracePointSubtype") or item_props.get("TracePointSubtypes", {})
                    schema_subtypes = tp_sub_prop.get("const", {})
                    allowed = list(dict.fromkeys(list(sub.get("enum", [])) + list(schema_subtypes.keys())))
                    allowed = [x for x in allowed if x]
                    if allowed:
                        formula = '"' + ",".join(map(str, allowed)) + '"'
                        dv = DataValidation(type="list", formula1=formula, allow_blank=True)
                        ws.add_data_validation(dv)
                        dv.add(range_ref)
                else:
                    enum_vals = _get_enum_for_dropdown(sub)
                    if enum_vals is not None:
                        formula = '"' + ",".join(map(str, enum_vals)) + '"'
                        dv = DataValidation(type="list", formula1=formula, allow_blank=True)
                        ws.add_data_validation(dv)
                        dv.add(range_ref)
                ws.column_dimensions[col_letter].width = max(len(str(h)) + 5, 18)

            ws.freeze_panes = "A2"

    try:
        wb.save(filename)
    except PermissionError:
        print(f"ERROR: Cannot write to '{filename}': File is locked or currently open in another application.")
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: Cannot write to '{filename}': {e}")
        sys.exit(1)


# =============================================================================
# Action Option Handlers
# =============================================================================

def check_output_file(filepath: str, force: bool = False) -> None:
    """Validate that the destination file can be written.

    If the file already exists:
      - When force is False: prints an error and exits without overwriting.
      - When force is True: checks whether the file is locked / open in another tool.
    Also checks if the file is locked by checking for LibreOffice lock files
    and attempting to open with write access.
    """
    p = Path(filepath)
    if p.exists():
        if not force:
            print(f"ERROR: Destination file '{filepath}' already exists. Use -f / --force to overwrite.")
            sys.exit(1)
        # Check for LibreOffice lock file (.~lock.<filename>#)
        lock_file = p.parent / f".~lock.{p.name}#"
        if lock_file.exists():
            print(f"ERROR: Cannot overwrite '{filepath}': File is locked or currently open in another application.")
            sys.exit(1)
        # Test if file can be opened for writing (detects exclusive locks from Excel, etc.)
        try:
            with open(filepath, "r+b"):
                pass
        except PermissionError:
            print(f"ERROR: Cannot overwrite '{filepath}': File is locked or currently open in another application.")
            sys.exit(1)
        except OSError as e:
            print(f"ERROR: Cannot overwrite '{filepath}': {e}")
            sys.exit(1)
    else:
        lock_file = p.parent / f".~lock.{p.name}#"
        if lock_file.exists():
            print(f"ERROR: Cannot write to '{filepath}': File is locked or currently open in another application.")
            sys.exit(1)


def opt_update_schema_hash(schema_path: str) -> None:
    schema = load_json(schema_path)
    calc_hash, is_valid = get_schema_hash(schema)

    if not is_valid:
        schema["hash"] = calc_hash
        save_json(schema_path, schema)
        print(f"Schema 'hash' updated to: {calc_hash}")
    else:
        print(f"Schema 'hash' is already up to date: {calc_hash}")


def opt_create_xlsx_from_schema(schema_path: str, output_path: str, force: bool = False) -> None:
    check_output_file(output_path, force)
    schema = load_json(schema_path)
    schema_hash = verify_schema_has_valid_hash(schema)
    build_xlsx_spreadsheet(schema, output_path, schema_hash)
    print(f"Created Excel spreadsheet: {output_path}")


def opt_create_ods_from_schema(schema_path: str, output_path: str, force: bool = False) -> None:
    check_output_file(output_path, force)
    schema = load_json(schema_path)
    schema_hash = verify_schema_has_valid_hash(schema)
    temp_xlsx = f"{output_path}_temp.xlsx"

    soffice = find_soffice()
    if soffice:
        build_xlsx_spreadsheet(schema, temp_xlsx, schema_hash)
        convert_xlsx_to_ods_soffice(temp_xlsx, soffice)
        if os.path.exists(temp_xlsx):
            try:
                os.remove(temp_xlsx)
            except OSError:
                pass
        temp_ods = f"{output_path}_temp.ods"
        if os.path.exists(temp_ods):
            try:
                os.replace(temp_ods, output_path)
            except PermissionError:
                print(f"ERROR: Cannot overwrite '{output_path}': File is locked or currently open in another application.")
                if os.path.exists(temp_ods):
                    try:
                        os.remove(temp_ods)
                    except OSError:
                        pass
                sys.exit(1)
            except OSError as e:
                print(f"ERROR: Cannot overwrite '{output_path}': {e}")
                if os.path.exists(temp_ods):
                    try:
                        os.remove(temp_ods)
                    except OSError:
                        pass
                sys.exit(1)
        disable_ods_show_page_breaks(output_path)
        print(f"Created ODS spreadsheet: {output_path}")
    else:
        print("INFO: LibreOffice not installed. Generating ODS directly.")
        create_ods_directly(schema, output_path, schema_hash)
        disable_ods_show_page_breaks(output_path)


def opt_validate_configuration(schema_path: str, config_path: str) -> None:
    schema = load_json(schema_path)
    schema_hash = verify_schema_has_valid_hash(schema)

    config = load_json(config_path)

    # 1. Check if schema hash and referenced schema hash in config match
    ref_schema_hash = config.get("schema_hash")
    if ref_schema_hash != schema_hash:
        # 2. If doesn't match, validation failed. Provide the reason why failed
        print(
            f"VALIDATION FAILED: Schema hash mismatch!\n"
            f"  Calculated Schema hash : {schema_hash}\n"
            f"  Config 'schema_hash'   : {ref_schema_hash}\n"
            f"Reason: The configuration references a schema hash that does not match the current schema hash. "
            f"The schema or config was modified without updating the reference."
        )
        sys.exit(1)

    # 3. If match, recalculate config hash and compare with existing config hash - this will detect any hand made change
    calc_cfg_hash = canonical_hash(config)
    stored_cfg_hash = config.get("hash")
    compromised = (stored_cfg_hash != calc_cfg_hash)

    # 4. If doesn't match, emit a warning that config has been compromised and continue to point 6.
    if compromised:
        print(
            f"WARNING: Config has been compromised!\n"
            f"  Stored 'hash'     : {stored_cfg_hash}\n"
            f"  Recalculated hash : {calc_cfg_hash}\n"
            f"The configuration content appears to have been modified manually without updating hash."
        )
    # 5. If match, continue to point 6.

    # 6. Validate all elements of config against schema.
    # 6a. JSON Schema structure validation
    to_validate = {k: v for k, v in config.items() if k not in ("hash", "schema_hash", "short_hash")}
    try:
        jsonschema.validate(instance=to_validate, schema=schema)
    except jsonschema.ValidationError as err:
        # 8. If validation not successful, then validation failed. Provide the reason why failed
        print("\nVALIDATION FAILED (JSON Schema):")
        print(err.message)
        if err.absolute_path:
            print("Path:", list(err.absolute_path))
        sys.exit(1)

    # 6b. Validate config elements against schema definitions (TracePointSubtype, TracePointIDs, types, IDs)
    tp_props = (
        schema.get("properties", {})
        .get("TracePoints", {})
        .get("items", {})
        .get("properties", {})
    )
    tp_sub_def = tp_props.get("TracePointSubtype") or tp_props.get("TracePointSubtypes", {})
    type_subtypes = tp_sub_def.get("const", {})
    allowed_types = set(type_subtypes.keys())
    if tp_props.get("TracePointType", {}).get("enum"):
        for t in tp_props["TracePointType"]["enum"]:
            if t:
                allowed_types.add(t)

    id_size_str = config.get("General", {}).get("TracePointIDSize", "2B")
    id_bytes = ID_SIZE_MAP.get(id_size_str, 2)
    max_id = (1 << (8 * id_bytes)) - 1
    reserved_sync_id = max_id

    tp_name_unique = config.get("General", {}).get("TracePointNameUnique", False)
    name_underscored = config.get("General", {}).get("TracePointNameUnderscored", False)
    prefix = config.get("General", {}).get("TracerPrefix", "TRC_")

    requires_atomic = any(tp.get("AtomicSectionRequired") is True for tp in config.get("TracePoints", []))
    if requires_atomic:
        gen = config.get("General", {})
        atomic_params = ["AtomicSectionStartAPI", "AtomicSectionStopAPI", "AtomicSectionHeader"]
        missing_atomic = [p for p in atomic_params if not gen.get(p) or not str(gen.get(p)).strip()]
        if missing_atomic:
            print(
                f"\nVALIDATION FAILED: At least one TracePoint requires an atomic section ('AtomicSectionRequired': true),\n"
                f"  but General is missing required parameter(s): {missing_atomic}.\n"
                f"  Please configure 'AtomicSectionStartAPI', 'AtomicSectionStopAPI', and 'AtomicSectionHeader' in General."
            )
            sys.exit(1)

    overrides = config.get("TracePointDataOverrides", [])
    if overrides:
        match_and_verify_overrides(overrides, config.get("TracePoints", []), type_subtypes, error_prefix="VALIDATION FAILED")

    stringify_rows = config.get("TracePointDataStringify", [])
    if stringify_rows:
        validate_stringify_table(stringify_rows, error_prefix="VALIDATION FAILED")

    validate_stringify_references(
        config.get("TracePoints", []),
        overrides,
        stringify_rows,
        error_prefix="VALIDATION FAILED",
    )

    seen_ids: dict[str, str] = {}
    seen_names: dict[str, int] = {}
    seen_full_names: dict[str, int] = {}

    for idx, tp in enumerate(config.get("TracePoints", [])):
        tp_name = tp.get("TracePointName", f"TracePoints[{idx}]")
        tp_grp = tp.get("TracePointGroup", "")
        raw_type = tp.get("TracePointType")
        tp_type = str(raw_type).strip() if raw_type is not None else ""

        tp_data = tp.get("TracePointData", [])
        tokens_to_validate = []
        if isinstance(tp_data, dict):
            for s_tokens in tp_data.values():
                if isinstance(s_tokens, list):
                    tokens_to_validate.extend(s_tokens)
        elif isinstance(tp_data, list):
            tokens_to_validate.extend(tp_data)

        for dt in tokens_to_validate:
            is_valid, err_msg = validate_data_token(dt)
            if not is_valid:
                print(
                    f"\nVALIDATION FAILED for TracePoint '{tp_name}':\n"
                    f"  {err_msg}"
                )
                sys.exit(1)

        if tp_name_unique:
            if tp_name in seen_names:
                print(
                    f"\nVALIDATION FAILED: Duplicate TracePointName '{tp_name}' detected.\n"
                    f"  First defined at index {seen_names[tp_name]}, duplicated at index {idx}.\n"
                    f"  General.TracePointNameUnique is set to true, requiring TracePointName to be unique across all trace points."
                )
                sys.exit(1)
            seen_names[tp_name] = idx

        # Determine expected subtypes according to schema
        if tp_type:
            if allowed_types and tp_type not in allowed_types:
                # 8. If validation not successful, provide reason why failed
                print(
                    f"\nVALIDATION FAILED for TracePoint '{tp_name}': unknown TracePointType '{tp_type}'.\n"
                    f"Allowed types in schema: {sorted(list(allowed_types))}"
                )
                sys.exit(1)
            expected_subtypes = type_subtypes.get(tp_type, [""])
            if not expected_subtypes:
                expected_subtypes = [""]
        else:
            # Blank or omitted TracePointType means single tracepoint without suffix (key "")
            expected_subtypes = [""]

        # Validate TracePointIDs
        tp_ids = tp.get("TracePointIDs")
        if not isinstance(tp_ids, dict):
            print(
                f"\nVALIDATION FAILED for TracePoint '{tp_name}': 'TracePointIDs' must be an object."
            )
            sys.exit(1)

        actual_subtypes = list(tp_ids.keys())

        if set(actual_subtypes) != set(expected_subtypes):
            missing = [s for s in expected_subtypes if s not in actual_subtypes]
            unexpected = [s for s in actual_subtypes if s not in expected_subtypes]
            reasons = []
            if missing:
                reasons.append(f"missing expected subtype(s): {[s if s else '(none)' for s in missing]}")
            if unexpected:
                reasons.append(f"unexpected subtype(s): {[s if s else '(none)' for s in unexpected]}")
            # 8. If validation not successful, provide reason why failed
            print(
                f"\nVALIDATION FAILED for TracePoint '{tp_name}':\n"
                f"  TracePointType           : '{tp_type or '(single/blank)'}'\n"
                f"  Schema expected subtypes : {[s if s else '(none)' for s in expected_subtypes]}\n"
                f"  Config TracePointIDs     : {[s if s else '(none)' for s in actual_subtypes]}\n"
                f"  Reason                   : {'; '.join(reasons)}"
            )
            sys.exit(1)

        # Validate ID values, range, reservation, and uniqueness
        for suffix, hex_val in tp_ids.items():
            parts = [p for p in [prefix, tp_grp, tp_name, tp_type, suffix] if p]
            label = "_".join(parts) if name_underscored else "".join(parts)

            if label in seen_full_names:
                print(
                    f"\nVALIDATION FAILED: Duplicate full trace point name '{label}' collision:\n"
                    f"  Used by trace point index {seen_full_names[label]} and index {idx}."
                )
                sys.exit(1)
            seen_full_names[label] = idx

            try:
                int_val = int(hex_val, 16)
            except (ValueError, TypeError):
                print(
                    f"\nVALIDATION FAILED for TracePoint '{tp_name}' [{suffix}]:\n"
                    f"  Invalid hex ID: {hex_val!r}"
                )
                sys.exit(1)

            if int_val < 0 or int_val > max_id:
                print(
                    f"\nVALIDATION FAILED for TracePoint '{tp_name}' [{suffix}]:\n"
                    f"  ID {hex_val} exceeds maximum allowed value 0x{max_id:X} for ID size '{id_size_str}'."
                )
                sys.exit(1)

            if int_val == reserved_sync_id:
                print(
                    f"\nVALIDATION FAILED for TracePoint '{tp_name}' [{suffix}]:\n"
                    f"  ID {hex_val} is reserved for EST_ID_SYNC."
                )
                sys.exit(1)

            norm_hex = format_tracepoint_id(int_val, id_bytes)
            if norm_hex in seen_ids:
                print(
                    f"\nVALIDATION FAILED: Duplicate TracePoint ID {norm_hex} collision:\n"
                    f"  Used by '{seen_ids[norm_hex]}' and '{label}'."
                )
                sys.exit(1)
            seen_ids[norm_hex] = label

    # 7. If validation successful, update config hash if a warning that config has been compromised was emit
    print("JSON config successfully validated against schema.")
    if compromised:
        config["hash"] = calc_cfg_hash
        calc_short_hash = format_short_hash(compute_32bit_hash(calc_cfg_hash))
        config["short_hash"] = calc_short_hash
        save_json(config_path, config)
        print(f"Updated config 'hash' to: {calc_cfg_hash}")
        print(f"Updated config 'short_hash' to: {calc_short_hash}")
        print(f"Saved updated configuration to: {config_path}")


def opt_import_config_to_xlsx(schema_path: str, config_path: str, output_path: str, force: bool = False) -> None:
    check_output_file(output_path, force)
    schema = load_json(schema_path)
    schema_hash = verify_schema_has_valid_hash(schema)

    config = load_json(config_path)
    calc_cfg_hash = canonical_hash(config)

    if config.get("hash") != calc_cfg_hash:
        print(f"ERROR: Config 'hash' mismatch or missing!\n  Stored    : {config.get('hash')}\n  Calculated: {calc_cfg_hash}")
        sys.exit(1)

    if config.get("schema_hash") != schema_hash:
        print(f"ERROR: Config 'schema_hash' ({config.get('schema_hash')}) does not match Schema hash ({schema_hash})!")
        sys.exit(1)

    calc_short_hash = format_short_hash(compute_32bit_hash(calc_cfg_hash))
    if config.get("short_hash") != calc_short_hash:
        print(f"ERROR: Config 'short_hash' ({config.get('short_hash')}) does not match the 32-bit fold of "
              f"'hash' ({calc_short_hash})!")
        sys.exit(1)

    build_xlsx_spreadsheet(schema, output_path, schema_hash, config=config, config_hash=calc_cfg_hash, short_hash=calc_short_hash)
    print(f"Created Excel spreadsheet: {output_path}")


def opt_import_config_to_ods(schema_path: str, config_path: str, output_path: str, force: bool = False) -> None:
    check_output_file(output_path, force)
    schema = load_json(schema_path)
    schema_hash = verify_schema_has_valid_hash(schema)

    config = load_json(config_path)
    calc_cfg_hash = canonical_hash(config)

    if config.get("hash") != calc_cfg_hash:
        print(f"ERROR: Config 'hash' mismatch or missing!\n  Stored    : {config.get('hash')}\n  Calculated: {calc_cfg_hash}")
        sys.exit(1)

    if config.get("schema_hash") != schema_hash:
        print(f"ERROR: Config 'schema_hash' ({config.get('schema_hash')}) does not match Schema hash ({schema_hash})!")
        sys.exit(1)

    calc_short_hash = format_short_hash(compute_32bit_hash(calc_cfg_hash))
    if config.get("short_hash") != calc_short_hash:
        print(f"ERROR: Config 'short_hash' ({config.get('short_hash')}) does not match the 32-bit fold of "
              f"'hash' ({calc_short_hash})!")
        sys.exit(1)

    soffice = find_soffice()

    if soffice:
        temp_xlsx = f"{output_path}_temp.xlsx"
        build_xlsx_spreadsheet(schema, temp_xlsx, schema_hash, config=config, config_hash=calc_cfg_hash, short_hash=calc_short_hash)
        convert_xlsx_to_ods_soffice(temp_xlsx, soffice)
        if os.path.exists(temp_xlsx):
            try:
                os.remove(temp_xlsx)
            except OSError:
                pass
        temp_ods = f"{output_path}_temp.ods"
        if os.path.exists(temp_ods):
            try:
                os.replace(temp_ods, output_path)
            except PermissionError:
                print(f"ERROR: Cannot overwrite '{output_path}': File is locked or currently open in another application.")
                if os.path.exists(temp_ods):
                    try:
                        os.remove(temp_ods)
                    except OSError:
                        pass
                sys.exit(1)
            except OSError as e:
                print(f"ERROR: Cannot overwrite '{output_path}': {e}")
                if os.path.exists(temp_ods):
                    try:
                        os.remove(temp_ods)
                    except OSError:
                        pass
                sys.exit(1)
        disable_ods_show_page_breaks(output_path)
        print(f"Created ODS spreadsheet: {output_path}")
    else:
        print("INFO: LibreOffice not installed. Generating ODS directly.")
        create_ods_directly(schema, output_path, schema_hash, config=config, config_hash=calc_cfg_hash, short_hash=calc_short_hash)
        disable_ods_show_page_breaks(output_path)


def opt_export_spreadsheet_configuration_to_json(schema_path: str, spreadsheet_path: str, output_prefix: str, force: bool = False) -> None:
    output_json = output_prefix if output_prefix.endswith(".json") else f"{output_prefix}.json"
    check_output_file(output_json, force)
    schema = load_json(schema_path)
    schema_hash = verify_schema_has_valid_hash(schema)

    ext = Path(spreadsheet_path).suffix.lower()
    if ext not in (".xlsx", ".ods"):
        print(f"ERROR: Invalid spreadsheet format '{ext}'. Only '.xlsx' and '.ods' files are supported.")
        sys.exit(1)

    root_props = schema.get("properties", {})
    config_data: dict[str, Any] = {"schema_hash": schema_hash}

    if ext == ".xlsx":
        wb = load_workbook(spreadsheet_path, data_only=True)
        sheets = wb.sheetnames
    else:
        if not HAS_ODF:
            print("ERROR: Reading ODS files requires pandas and odfpy (pip install pandas odfpy).")
            sys.exit(1)
        excel_file = pd.ExcelFile(spreadsheet_path, engine="odf")
        sheets = excel_file.sheet_names

    def get_sheet_rows(sheet_name: str) -> list[list[Any]]:
        if ext == ".xlsx":
            ws = wb[sheet_name]
            return [[cell.value for cell in row] for row in ws.iter_rows()]
        else:
            df = pd.read_excel(excel_file, sheet_name=sheet_name, header=None, engine="odf")
            return df.values.tolist()

    def coerce_value(val: Any, prop_schema: dict) -> Any:
        expected_type = prop_schema.get("type")
        if not expected_type and "oneOf" in prop_schema:
            for branch in prop_schema["oneOf"]:
                if branch.get("type") == "array":
                    expected_type = "array"
                    break
        elif not expected_type and "anyOf" in prop_schema:
            for branch in prop_schema["anyOf"]:
                if branch.get("type") == "array":
                    expected_type = "array"
                    break

        # Handle NaN from pandas or None from openpyxl
        if val is None or (HAS_ODF and pd.isna(val)):
            if expected_type == "array":
                return []
            elif expected_type == "string":
                return ""
            return None

        if expected_type == "boolean":
            if isinstance(val, bool):
                return val
            return str(val).strip().lower() in ("true", "1", "yes")

        elif expected_type == "integer":
            try:
                return int(val)
            except (ValueError, TypeError):
                return str(val).strip()

        elif expected_type == "array":
            if isinstance(val, list):
                return [x for x in val if x and not (HAS_ODF and pd.isna(x))]
            val_str = str(val).strip()
            if not val_str or val_str.lower() == "nan":
                return []
            return [x.strip() for x in re.split(r'[,; ]+', val_str) if x.strip() and x.strip().lower() != "nan"]

        else:
            val_str = str(val).strip()
            return "" if val_str.lower() == "nan" else val_str

    for p_name, p_schema in root_props.items():
        if p_name not in sheets:
            continue

        rows = get_sheet_rows(p_name)
        if not rows:
            continue

        p_type = p_schema.get("type")

        if p_type == "object":
            sub_props = p_schema.get("properties", {})
            obj_data = {}
            for row in rows[1:]:  # skip header row
                if not row or len(row) < 2 or row[0] is None:
                    continue
                k = str(row[0]).strip()
                v = row[1]
                if k in sub_props:
                    obj_data[k] = coerce_value(v, sub_props[k])
            config_data[p_name] = obj_data

        elif p_type == "array" and p_schema.get("items", {}).get("type") == "object":
            item_schema = p_schema.get("items", {})
            item_props = item_schema.get("properties", {})
            item_req = item_schema.get("required", [])
            headers = [str(h).strip() if h is not None else "" for h in rows[0]]
            arr_data = []

            for row in rows[1:]:
                if not row or all(c is None or str(c).strip() == "" for c in row):
                    continue
                item_obj = {}
                for idx, h in enumerate(headers):
                    if h in item_props and idx < len(row):
                        v = row[idx]
                        coerced = coerce_value(v, item_props[h])
                        if h not in item_req and (coerced is None or coerced == ""):
                            continue
                        item_obj[h] = coerced
                if item_obj:
                    arr_data.append(item_obj)
            config_data[p_name] = arr_data

    # Compute and inject TracePointIDs. These never come from the spreadsheet --
    # each one is a pure function of TracePointGroup/TracePointName/suffix, so it's
    # always reproducible and there's nothing for a human to usefully edit. This
    # runs before validation below, since TracePointIDs is a required schema field.
    id_size_str = config_data.get("General", {}).get("TracePointIDSize")
    id_bytes = ID_SIZE_MAP.get(id_size_str)
    if id_bytes is None:
        print(f"ERROR: General.TracePointIDSize is missing or invalid ({id_size_str!r}); "
              f"cannot compute TracePointIDs. Allowed: {', '.join(ID_SIZE_MAP.keys())}.")
        sys.exit(1)

    # Load subtype definitions from the schema's TracePointSubtype property (inside properties, so it is schema-hashed).
    tp_props = (
        schema
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

    reserved_sync_id = (1 << (id_bytes * 8)) - 1
    generated_ids: dict[int, str] = {}
    seen_names: dict[str, int] = {}
    seen_full_names: dict[str, int] = {}
    tp_name_unique = config_data.get("General", {}).get("TracePointNameUnique", False)
    name_underscored = config_data.get("General", {}).get("TracePointNameUnderscored", False)
    prefix = config_data.get("General", {}).get("TracerPrefix", "TRC_")

    requires_atomic = any(tp.get("AtomicSectionRequired") is True for tp in config_data.get("TracePoints", []))
    if requires_atomic:
        gen = config_data.get("General", {})
        atomic_params = ["AtomicSectionStartAPI", "AtomicSectionStopAPI", "AtomicSectionHeader"]
        missing_atomic = [p for p in atomic_params if not gen.get(p) or not str(gen.get(p)).strip()]
        if missing_atomic:
            print(
                f"\nSPREADSHEET VALIDATION ERROR: At least one TracePoint requires an atomic section ('AtomicSectionRequired': true),\n"
                f"  but General is missing required parameter(s): {missing_atomic}."
            )
            sys.exit(1)

    overrides = config_data.get("TracePointDataOverrides", [])
    if overrides:
        match_and_verify_overrides(overrides, config_data.get("TracePoints", []), type_subtypes, error_prefix="SPREADSHEET VALIDATION ERROR")

    # Read TracePointDataStringify sheet (Name, Values, Stringify columns).
    # This sheet is not part of the generic schema-driven loop above because its
    # Name column holds definition-side entries (plain or with [range]) that are
    # stored verbatim -- there is no type coercion needed.
    stringify_rows: list[dict] = []
    STRINGIFY_SHEET = "TracePointDataStringify"
    if STRINGIFY_SHEET in sheets:
        s_rows = get_sheet_rows(STRINGIFY_SHEET)
        if s_rows and len(s_rows) > 1:
            s_headers = [str(h).strip() if h is not None else "" for h in s_rows[0]]
            s_cols = {"Name", "Values", "Stringify"}
            for s_row in s_rows[1:]:
                if not s_row or all(c is None or str(c).strip() == "" for c in s_row):
                    continue
                row_obj = {}
                for col_idx, col_name in enumerate(s_headers):
                    if col_name in s_cols and col_idx < len(s_row):
                        v = s_row[col_idx]
                        row_obj[col_name] = "" if (v is None or (HAS_ODF and pd.isna(v))) else str(v).strip()
                if any(row_obj.get(c, "") for c in s_cols):
                    stringify_rows.append(row_obj)
    config_data[STRINGIFY_SHEET] = stringify_rows

    if stringify_rows:
        validate_stringify_table(stringify_rows, error_prefix="SPREADSHEET VALIDATION ERROR")

    validate_stringify_references(
        config_data.get("TracePoints", []),
        overrides,
        stringify_rows,
        error_prefix="SPREADSHEET VALIDATION ERROR",
    )

    for idx, tp in enumerate(config_data.get("TracePoints", [])):
        grp = tp.get("TracePointGroup", "")
        name = tp.get("TracePointName", "")
        raw_type = tp.get("TracePointType")
        tp_type = str(raw_type).strip() if raw_type is not None else ""

        if tp_name_unique:
            if name in seen_names:
                print(
                    f"\nFATAL ERROR: Duplicate TracePointName '{name}' detected across trace points!\n"
                    f"  Trace point #{idx + 1} duplicates '{name}' (first seen at #{seen_names[name] + 1}).\n"
                    f"  General.TracePointNameUnique is true, requiring TracePointName to be unique."
                )
                sys.exit(1)
            seen_names[name] = idx

        if not tp_type:
            # TracePointType is optional: if omitted or blank, generate single trace point (key "")
            tp.pop("TracePointType", None)
            suffixes = [""]
        else:
            if allowed_types and tp_type not in allowed_types:
                print(f"\nERROR: TracePoint '{name}' has unrecognized TracePointType '{tp_type}'.")
                print(f"  Allowed types defined in schema: {sorted(list(allowed_types))}")
                sys.exit(1)
            suffixes = type_subtypes.get(tp_type, [""])
            if not suffixes:
                suffixes = [""]
            tp["TracePointType"] = tp_type

        # Apply TracePointDataOverrides
        base_data = tp.get("TracePointData", [])
        data_by_suffix: dict[str, list] = {}
        for suff in suffixes:
            matching_rows = []
            for r_idx, row in enumerate(overrides, start=1):
                f_kind = row.get("TracePointFilterKind", "wildcards")
                grp_f = str(row.get("TracePointGroupFilter", ""))
                name_f = str(row.get("TracePointNameFilter", ""))
                type_f = str(row.get("TracePointTypeFilter", ""))
                sub_f = str(row.get("TracePointSubtypeFilter", ""))
                if (match_filter(f_kind, grp_f, grp) and
                    match_filter(f_kind, name_f, name) and
                    match_filter(f_kind, type_f, tp_type) and
                    match_filter(f_kind, sub_f, suff)):
                    matching_rows.append((r_idx, row))

            if len(matching_rows) >= 1:
                first_data = list(matching_rows[0][1].get("TracePointDataOverride", []))
                if any(list(r[1].get("TracePointDataOverride", [])) != first_data for r in matching_rows[1:]):
                    conflicts_desc = [f"Row {r[0]} ({r[1].get('TracePointDataOverride', [])})" for r in matching_rows]
                    print(
                        f"\nSPREADSHEET VALIDATION ERROR: Conflicting data overrides for trace point '{grp}{name}' "
                        f"(type '{tp_type or '(none)'}', subtype '{suff or '(none)'}')!\n"
                        f"  Matches multiple override rows with differing data: {', '.join(conflicts_desc)}."
                    )
                    sys.exit(1)
                data_by_suffix[suff] = list(first_data)
            else:
                data_by_suffix[suff] = list(base_data)

        # If all suffixes have identical data, store as list; otherwise as dict
        first_data = data_by_suffix[suffixes[0]]
        if all(data_by_suffix[s] == first_data for s in suffixes):
            tp["TracePointData"] = list(first_data)
        else:
            tp["TracePointData"] = data_by_suffix

        # Validate resolved data tokens
        tokens_to_check = []
        if isinstance(tp["TracePointData"], dict):
            for s_toks in tp["TracePointData"].values():
                tokens_to_check.extend(s_toks)
        else:
            tokens_to_check.extend(tp["TracePointData"])

        for tok in tokens_to_check:
            is_valid, err_msg = validate_data_token(tok)
            if not is_valid:
                print(
                    f"\nSPREADSHEET VALIDATION ERROR for TracePoint '{name}':\n"
                    f"  {err_msg}"
                )
                sys.exit(1)

        core_str = tp.get("TracePointCore") or ""
        if core_str and core_str not in ("1B", "2B"):
            import re as _re
            if not _re.fullmatch(r'(0|[1-9][0-9]*)', core_str):
                print(
                    f"\nSPREADSHEET VALIDATION ERROR for TracePoint '{name}':\n"
                    f"  Invalid TracePointCore '{core_str}'.\n"
                    f"  Allowed: '1B', '2B', a non-negative decimal integer (static core), or '' (none)."
                )
                sys.exit(1)

        tp_ids: dict[str, str] = {}
        for suffix in suffixes:
            tp_id = compute_tracepoint_id(grp, name, suffix, id_bytes)
            parts = [p for p in [prefix, grp, name, tp_type, suffix] if p]
            macro_label = "_".join(parts) if name_underscored else "".join(parts)

            if macro_label in seen_full_names:
                print(
                    f"\nFATAL ERROR: Duplicate full trace point name '{macro_label}' detected!\n"
                    f"  Used by trace point #{seen_full_names[macro_label] + 1} and #{idx + 1}."
                )
                sys.exit(1)
            seen_full_names[macro_label] = idx

            if tp_id == reserved_sync_id:
                print(f"\nFATAL ERROR: Trace point '{macro_label}' generated the reserved "
                      f"sync ID 0x{tp_id:0{id_bytes * 2}X}. Rename this trace point.")
                sys.exit(1)
            if tp_id in generated_ids:
                print(f"\nFATAL ERROR: ID Collision Detected (0x{tp_id:0{id_bytes * 2}X})!")
                print(f"Trace point '{macro_label}' collides with '{generated_ids[tp_id]}'.")
                sys.exit(1)
            generated_ids[tp_id] = macro_label

            tp_ids[suffix] = format_tracepoint_id(tp_id, id_bytes)

        tp["TracePointIDs"] = tp_ids

    # Validate configuration against JSON Schema
    to_validate = {k: v for k, v in config_data.items() if k not in ("hash", "schema_hash", "short_hash")}
    try:
        jsonschema.validate(instance=to_validate, schema=schema)
        print("Spreadsheet configuration validated against schema.")
    except jsonschema.ValidationError as err:
        print("\nSPREADSHEET VALIDATION ERROR:")
        print(err.message)
        sys.exit(1)

    # Compute canonical config hash (ignoring hash, schema_hash, short_hash) and the
    # 32-bit short_hash derived from it
    calc_cfg_hash = canonical_hash(config_data)
    config_data["hash"] = calc_cfg_hash
    config_data["short_hash"] = format_short_hash(compute_32bit_hash(calc_cfg_hash))

    # Save output JSON file
    output_json = output_prefix if output_prefix.endswith(".json") else f"{output_prefix}.json"
    save_json(output_json, config_data)
    print(f"Exported spreadsheet configuration to JSON file: {output_json}")

# =============================================================================
# CLI Main Argument Parsing
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="manage_est_json_io.py",
        description="Unified tool replacing extract_config.py, gen_trc_tpl.py, and import_config_to_tpl.py.",
        formatter_class=argparse.RawTextHelpFormatter
    )

    action_group = parser.add_mutually_exclusive_group(required=True)

    action_group.add_argument(
        "--update_schema_hash", "-ush",
        nargs=1,
        metavar=("SCHEMA_JSON"),
        help="Updates/adds canonical hash on schema properties (whitespace & ordering independent)."
    )
    action_group.add_argument(
        "--create_xslx_spreadsheet_from_schema", "-cxsfs",
        nargs=2,
        metavar=("SCHEMA_JSON", "OUTPUT_XLSX"),
        help="Checks schema hash and generates an empty Excel template (.xlsx)."
    )
    action_group.add_argument(
        "--create_ods_spreadsheet_from_schema", "-cosfs",
        nargs=2,
        metavar=("SCHEMA_JSON", "OUTPUT_ODS"),
        help="Checks schema hash and generates an empty ODS template (.ods)."
    )
    action_group.add_argument(
        "--validate_configuration", "-vc",
        nargs=2,
        metavar=("FILE_1", "FILE_2"),
        help="Validates JSON config against schema and updates config hash and schema_hash.\n(Accepts schema and config in any order)."
    )
    action_group.add_argument(
        "--import_configuration_to_xlsx_spreadsheet", "-ictxs",
        nargs=3,
        metavar=("FILE_1", "FILE_2", "FILE_3"),
        help="Verifies all hashes and writes filled JSON config into an Excel file (.xlsx).\n(Accepts schema, config, and output .xlsx in any order)."
    )
    action_group.add_argument(
        "--import_configuration_to_ods_spreadsheet", "-ictos",
        nargs=3,
        metavar=("FILE_1", "FILE_2", "FILE_3"),
        help="Verifies all hashes and writes filled JSON config into an ODS file (.ods).\n(Accepts schema, config, and output .ods in any order)."
    )
    action_group.add_argument(
        "--export_spreadsheet_configuration_to_json", "-esctj",
        nargs=3,
        metavar=("FILE_1", "FILE_2", "FILE_3"),
        help="Checks schema hash, consistency against schema in spreadsheet, and generates JSON config file.\n(Accepts schema, spreadsheet, and output .json in any order)."
    )

    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force overwrite of existing destination files."
    )

    args = parser.parse_args()

    def check_ext(filepath: str, allowed: tuple[str, ...], role: str) -> str:
        ext = Path(filepath).suffix.lower()
        if ext not in allowed:
            print(f"ERROR: Expected a {role} file with extension {allowed}, got '{filepath}'.")
            sys.exit(1)
        return filepath

    if args.update_schema_hash:
        schema_file = check_ext(args.update_schema_hash[0], (".json",), "schema JSON")
        opt_update_schema_hash(schema_file)

    elif args.create_xslx_spreadsheet_from_schema:
        raw_a, raw_b = args.create_xslx_spreadsheet_from_schema
        # Find which is .json (schema) and which is .xlsx (output)
        if Path(raw_a).suffix.lower() == ".json" and Path(raw_b).suffix.lower() == ".xlsx":
            schema_f, out_f = raw_a, raw_b
        elif Path(raw_b).suffix.lower() == ".json" and Path(raw_a).suffix.lower() == ".xlsx":
            schema_f, out_f = raw_b, raw_a
        else:
            schema_f = check_ext(raw_a, (".json",), "schema JSON")
            out_f = check_ext(raw_b, (".xlsx",), "output Excel (.xlsx)")
        opt_create_xlsx_from_schema(schema_f, out_f, force=args.force)

    elif args.create_ods_spreadsheet_from_schema:
        raw_a, raw_b = args.create_ods_spreadsheet_from_schema
        if Path(raw_a).suffix.lower() == ".json" and Path(raw_b).suffix.lower() == ".ods":
            schema_f, out_f = raw_a, raw_b
        elif Path(raw_b).suffix.lower() == ".json" and Path(raw_a).suffix.lower() == ".ods":
            schema_f, out_f = raw_b, raw_a
        else:
            schema_f = check_ext(raw_a, (".json",), "schema JSON")
            out_f = check_ext(raw_b, (".ods",), "output ODS (.ods)")
        opt_create_ods_from_schema(schema_f, out_f, force=args.force)

    elif args.validate_configuration:
        raw_a, raw_b = args.validate_configuration
        check_ext(raw_a, (".json",), "JSON")
        check_ext(raw_b, (".json",), "JSON")
        schema_f, config_f = resolve_schema_and_config(raw_a, raw_b)
        opt_validate_configuration(schema_f, config_f)

    elif args.import_configuration_to_xlsx_spreadsheet:
        raw_args = list(args.import_configuration_to_xlsx_spreadsheet)
        # We need 2 JSON files and 1 .xlsx file
        xlsx_files = [f for f in raw_args if Path(f).suffix.lower() == ".xlsx"]
        json_files = [f for f in raw_args if Path(f).suffix.lower() == ".json"]
        if len(xlsx_files) != 1 or len(json_files) != 2:
            print("ERROR: --import_configuration_to_xlsx_spreadsheet requires 2 .json files (schema and config) and 1 .xlsx output file.")
            sys.exit(1)
        schema_f, config_f = resolve_schema_and_config(json_files[0], json_files[1])
        out_f = xlsx_files[0]
        opt_import_config_to_xlsx(schema_f, config_f, out_f, force=args.force)

    elif args.import_configuration_to_ods_spreadsheet:
        raw_args = list(args.import_configuration_to_ods_spreadsheet)
        # We need 2 JSON files and 1 .ods file
        ods_files = [f for f in raw_args if Path(f).suffix.lower() == ".ods"]
        json_files = [f for f in raw_args if Path(f).suffix.lower() == ".json"]
        if len(ods_files) != 1 or len(json_files) != 2:
            print("ERROR: --import_configuration_to_ods_spreadsheet requires 2 .json files (schema and config) and 1 .ods output file.")
            sys.exit(1)
        schema_f, config_f = resolve_schema_and_config(json_files[0], json_files[1])
        out_f = ods_files[0]
        opt_import_config_to_ods(schema_f, config_f, out_f, force=args.force)

    elif args.export_spreadsheet_configuration_to_json:
        raw_args = list(args.export_spreadsheet_configuration_to_json)
        # We need 1 spreadsheet (.xlsx or .ods) and 2 .json files (schema input and config output)
        spreadsheets = [f for f in raw_args if Path(f).suffix.lower() in (".xlsx", ".ods")]
        json_files = [f for f in raw_args if Path(f).suffix.lower() == ".json"]
        if len(spreadsheets) != 1 or len(json_files) != 2:
            print("ERROR: --export_spreadsheet_configuration_to_json requires 1 spreadsheet (.xlsx or .ods) and 2 .json files (schema and output config).")
            sys.exit(1)
        # Disambiguate which JSON is existing schema vs target output
        j1, j2 = json_files
        if is_schema_json(j1) and not is_schema_json(j2):
            schema_f, out_json = j1, j2
        elif is_schema_json(j2) and not is_schema_json(j1):
            schema_f, out_json = j2, j1
        elif "schema" in Path(j1).name.lower():
            schema_f, out_json = j1, j2
        elif "schema" in Path(j2).name.lower():
            schema_f, out_json = j2, j1
        else:
            schema_f, out_json = j1, j2
        sheet_f = spreadsheets[0]
        opt_export_spreadsheet_configuration_to_json(schema_f, sheet_f, out_json, force=args.force)


if __name__ == "__main__":
    main()