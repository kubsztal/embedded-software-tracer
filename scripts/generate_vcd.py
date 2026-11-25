#!/usr/bin/env python3
"""
Policy-Driven IEEE 1364 VCD Generator.
Supports multi/single FIFO/FILO policies, argument stringification, and all 17 Verilog variable types.
"""

import argparse
from datetime import date
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union
import yaml


# ==============================================================================
# 1. VCD ASCII SYMBOL ALLOCATOR
# ==============================================================================
class VCDSymbolGenerator:
    """Generates unique printable ASCII identifiers for VCD variables."""

    def __init__(self):
        self._current_id = 33  # ASCII '!'

    def next_symbol(self) -> str:
        symbol = ""
        current = self._current_id
        while True:
            symbol = chr(33 + (current - 33) % 94) + symbol
            current = 33 + (current - 33) // 94 - 1
            if current < 33:
                break
        self._current_id += 1
        return symbol


# ==============================================================================
# 2. VERILOG VARIABLE TYPES & VALUE FORMATTING
# ==============================================================================
VERILOG_NATURAL_SIZES = {
    "event": 1,
    "string": 1,
    "integer": 32,
    "parameter": 16,
    "time": 64,
    "real": 64,
    "realtime": 64,
    "wire": 1,
    "reg": 1,
    "supply0": 1,
    "supply1": 1,
    "tri": 1,
    "trireg": 1,
    "tri0": 1,
    "tri1": 1,
    "wand": 1,
    "wor": 1,
    "triand": 1,
    "trior": 1,
}

_SINGLE_BIT_VALS = frozenset("01xXzZhHlLuUwW-")


def resolve_var_type_and_size(var_type_str: str) -> Tuple[str, int]:
    """
    Parses a var_type string (e.g. 'wire 1', 'reg 128', 'real 32', 'integer', 'reg [127:0]')
    and returns (base_type, clamped_size). Supports all 17 Verilog types + string.
    Size is sanitized and clamped to the valid VCD scope [1, 65536].
    """
    s = var_type_str.strip()
    parts = s.split()
    base_type = parts[0].lower() if parts else "wire"

    # Check for bit-range notation like [127:0] or [0:7]
    range_match = re.search(r"\[(\d+):(\d+)\]", s)
    if range_match:
        size = abs(int(range_match.group(1)) - int(range_match.group(2))) + 1
    else:
        size = None
        for p in parts[1:]:
            if p.isdigit():
                size = int(p)
                break
        if size is None:
            size = VERILOG_NATURAL_SIZES.get(base_type, 1)

    # Clamping to valid VCD scope [1, 65536]
    size = max(1, min(size, 65536))
    return base_type, size


def is_null_val(val: Any) -> bool:
    """Returns True if val represents null/None (meaning 'does not exist, do not generate')."""
    if val is None:
        return True
    if isinstance(val, str) and val.strip().lower() in ("null", "none"):
        return True
    return False


def format_vcd_value(val: Any, symbol: str, var_type: str) -> Optional[str]:
    """
    Formats a value for VCD output based on variable type and value characteristics:
    - Null/None values: returns None ('does not exist, do not generate')
    - String variables: s<val> <symbol>
    - Real variables: r<val> <symbol>
    - Single-bit values (0, 1, x, z, h, l, u, w, -) on 1-bit vars: <val><symbol>
    - Shorthand unknown/high-impedance on multi-bit vars: <val><symbol>
    - Multi-bit integer/vector: b<bits> <symbol>
    - Unrecognized values emit a warning to stderr and sanitize to a standard fallback.
    """
    if is_null_val(val):
        return None

    vt = var_type.strip().lower()
    base_type, size = resolve_var_type_and_size(vt)
    s = str(val).strip()

    if "string" in vt or base_type == "string":
        return f"s{s} {symbol}"

    if base_type in ("real", "realtime"):
        try:
            float(s)
            return f"r{s} {symbol}"
        except ValueError:
            sys.stderr.write(
                f"Warning: Real value '{val}' for symbol '{symbol}' ({var_type}) "
                f"is not a valid number; sanitized to 'r0.0'\n"
            )
            return f"r0.0 {symbol}"

    if size == 1:
        if s in _SINGLE_BIT_VALS:
            return f"{s}{symbol}"
        else:
            sys.stderr.write(
                f"Warning: 1-bit value '{val}' for symbol '{symbol}' ({var_type}) "
                f"is not recognized (allowed: 0, 1, x, X, z, Z, h, H, l, L, u, U, w, W, -); "
                f"sanitized to 'x'\n"
            )
            return f"x{symbol}"

    # Shorthand for bus vectors: x, z, u, etc. apply to all bits
    if size > 1 and s in ("x", "X", "z", "Z", "u", "U", "w", "W", "-"):
        return f"{s}{symbol}"

    try:
        v_int = int(val)
        if v_int >= 0:
            return f"b{v_int:b} {symbol}"
        else:
            return f"b{v_int & ((1 << size) - 1):b} {symbol}"
    except (ValueError, TypeError):
        pass

    sys.stderr.write(
        f"Warning: Vector value '{val}' for symbol '{symbol}' ({var_type}) "
        f"is not a valid integer or recognized shorthand; sanitized to 'bx'\n"
    )
    return f"bx {symbol}"


COUNTER_SUPPORTED_BASE_TYPES = {"real", "realtime", "integer", "string"}


def check_counter_var_type(var_type_str: str, identifier: str) -> str:
    """Validates that counter var_type is supported (real, integer, string), else warns and falls back to real 64."""
    base_type, _ = resolve_var_type_and_size(var_type_str)
    if base_type not in COUNTER_SUPPORTED_BASE_TYPES:
        sys.stderr.write(
            f"Warning: Counter policy on '{identifier}' does not support var_type '{var_type_str}'. "
            f"Supported types are real, integer, string. Falling back to 'real 64'.\n"
        )
        return "real 64"
    return var_type_str


# ==============================================================================
# 3. CONCURRENCY POLICY TRACKER
# ==============================================================================
class PolicyTracker:
    """Manages active dynamic slots, reference counts, or counter values based on policy."""

    def __init__(
        self,
        policy: str,
        idle_val: Optional[str] = "z",
        active_val: Optional[str] = "x",
        init_val: Any = "0",
    ):
        self.policy = policy.lower()
        self.idle_val = idle_val
        self.active_val = active_val

        # State for multi-trace policies
        self.active_slots: List[int] = []
        self.max_depth_seen: int = 0

        # State for single-trace policies
        self.is_active: bool = False
        self.active_count: int = 0

        # State for counter policy
        try:
            self.initial_count = int(init_val) if init_val is not None else 0
        except (ValueError, TypeError):
            self.initial_count = 0
        self.count: int = self.initial_count

    def reset(self):
        """Resets execution state between Pass 1 and Pass 2."""
        self.active_slots.clear()
        self.is_active = False
        self.active_count = 0
        self.count = self.initial_count

    def execute_action(self, action: str) -> Optional[Tuple[int, Any]]:
        # A. MULTI_TRACE_FIFO
        if self.policy == "multi_trace_fifo":
            if action in ("activate", "increase"):
                slot = 1
                while slot in self.active_slots:
                    slot += 1
                self.active_slots.append(slot)
                if slot > self.max_depth_seen:
                    self.max_depth_seen = slot
                return slot, self.active_val

            elif action in ("deactivate", "decrease"):
                if not self.active_slots:
                    return None
                slot = self.active_slots.pop(0)  # Release oldest slot
                return slot, self.idle_val

        # B. MULTI_TRACE_FILO
        elif self.policy == "multi_trace_filo":
            if action in ("activate", "increase"):
                slot = len(self.active_slots) + 1
                self.active_slots.append(slot)
                if slot > self.max_depth_seen:
                    self.max_depth_seen = slot
                return slot, self.active_val

            elif action in ("deactivate", "decrease"):
                if not self.active_slots:
                    return None
                slot = self.active_slots.pop()  # Release top/newest slot
                return slot, self.idle_val

        # C. SINGLE_TRACE_FIFO (1st activation -> ON, 1st deactivation -> OFF)
        elif self.policy == "single_trace_fifo":
            self.max_depth_seen = max(1, self.max_depth_seen)
            if action in ("activate", "increase"):
                if not self.is_active:
                    self.is_active = True
                    return 1, self.active_val
            elif action in ("deactivate", "decrease"):
                if self.is_active:
                    self.is_active = False
                    return 1, self.idle_val

        # D. SINGLE_TRACE_FILO (1st activation -> ON, LAST deactivation -> OFF)
        elif self.policy == "single_trace_filo":
            self.max_depth_seen = max(1, self.max_depth_seen)
            if action in ("activate", "increase"):
                self.active_count += 1
                if self.active_count == 1:
                    return 1, self.active_val
            elif action in ("deactivate", "decrease"):
                if self.active_count > 0:
                    self.active_count -= 1
                    if self.active_count == 0:
                        return 1, self.idle_val

        # E. COUNTER (increments on increase, decrements on decrease)
        elif self.policy == "counter":
            self.max_depth_seen = max(1, self.max_depth_seen)
            if action in ("increase", "activate"):
                self.count += 1
                return 1, self.count
            elif action in ("decrease", "deactivate"):
                self.count -= 1
                return 1, self.count

        return None


# ==============================================================================
# 4. CONFIG LOOKUP & CONTEXT HELPERS
# ==============================================================================
def normalize_id(id_val: Union[str, int]) -> str:
    """Normalizes an ID to uppercase hex string (e.g. '0xB413')."""
    if isinstance(id_val, int):
        return f"0x{id_val:X}"
    s = str(id_val).strip()
    if s.lower().startswith("0x"):
        try:
            return f"0x{int(s, 16):X}"
        except ValueError:
            return s.upper()
    return s.upper()


def build_id_lookup(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Builds hex_id -> {prefix, group, name, tp_type, subtype, underscored} lookup from config."""
    gen = config.get("General", {})
    prefix = gen.get("TracerPrefix", "")
    underscored = gen.get("TracePointNameUnderscored", False)
    lookup: Dict[str, Dict[str, Any]] = {}

    for tp in config.get("TracePoints", []):
        group = tp.get("TracePointGroup", "")
        name = tp.get("TracePointName", "")
        tp_type = tp.get("TracePointType", "")
        tp_ids = tp.get("TracePointIDs") or {}
        for suffix, hex_id in tp_ids.items():
            if hex_id:
                norm_key = normalize_id(hex_id)
                lookup[norm_key] = {
                    "prefix": prefix,
                    "group": group,
                    "name": name,
                    "tp_type": tp_type,
                    "subtype": suffix,
                    "underscored": underscored,
                }
    return lookup


def build_context(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Builds template context dictionary from an entry dict."""
    underscored = entry.get("underscored", True)
    sep = "_" if underscored else ""
    prefix = entry.get("prefix", "")
    group = entry.get("group", "")
    name = entry.get("name", "")
    tp_type = entry.get("tp_type", "")
    subtype = entry.get("subtype", "")

    parts_full = [p for p in [prefix, group, name, tp_type, subtype] if p]
    parts_base = [p for p in [prefix, group, name] if p]

    return {
        "prefix": prefix,
        "group": group,
        "name": name,
        "type": tp_type,
        "subtype": subtype,
        "full_name": sep.join(parts_full),
        "base_name": sep.join(parts_base),
    }


def flatten_data(data_list: List[Any]) -> List[Dict[str, Any]]:
    """Flattens tracepoint Data list into a list of {arg_idx, array_idx, val, stringify}."""
    result: List[Dict[str, Any]] = []
    arg_idx = 1
    for item in data_list:
        if isinstance(item, dict):
            val = item.get("Value")
            stringify = item.get("Stringify")
        else:
            val = item
            stringify = None

        if isinstance(val, list):
            for a_idx, v in enumerate(val):
                s = None
                if isinstance(stringify, list) and a_idx < len(stringify):
                    s = stringify[a_idx]
                elif isinstance(stringify, str):
                    s = stringify
                result.append({
                    "arg_idx": arg_idx,
                    "array_idx": a_idx,
                    "val": v,
                    "stringify": s,
                })
        else:
            result.append({
                "arg_idx": arg_idx,
                "array_idx": None,
                "val": val,
                "stringify": stringify,
            })
        arg_idx += 1
    return result


def validate_inputs(schema_path: str, config_path: str, trace_path: str):
    """Validates that input files exist and have the expected extensions."""
    for p, desc in [(schema_path, "VCD schema"), (config_path, "config"), (trace_path, "trace JSON")]:
        if not os.path.isfile(p):
            raise FileNotFoundError(f"{desc} file does not exist: '{p}'")

    schema_ext = os.path.splitext(schema_path)[1].lower()
    if schema_ext not in (".yaml", ".yml"):
        raise ValueError(
            f"--vcd-schema must be a YAML file (.yaml or .yml), got: '{schema_path}'. "
            f"Do not pass a JSON file (such as config_schema.json) to --vcd-schema."
        )

    config_ext = os.path.splitext(config_path)[1].lower()
    if config_ext != ".json":
        raise ValueError(
            f"--config must be a JSON file (.json), got: '{config_path}'"
        )

    trace_ext = os.path.splitext(trace_path)[1].lower()
    if trace_ext != ".json":
        raise ValueError(
            f"--trace-json must be a JSON file (.json), got: '{trace_path}'"
        )


def validate_vcd_schema(schema: Any, schema_name: str = "vcd_schema"):
    """Validates that the parsed YAML schema conforms to the expected VCD schema structure."""
    if not isinstance(schema, dict):
        raise ValueError(
            f"Invalid VCD schema in '{schema_name}': expected a YAML dictionary mapping at root."
        )
    if "tracepoints" not in schema:
        raise ValueError(
            f"Invalid VCD schema in '{schema_name}': missing mandatory root key 'tracepoints'."
        )
    tp_cfg = schema.get("tracepoints")
    if not isinstance(tp_cfg, dict):
        raise ValueError(
            f"Invalid VCD schema in '{schema_name}': 'tracepoints' must be a dictionary."
        )
    if not ("basic_layer" in tp_cfg or "derived_layer" in tp_cfg):
        raise ValueError(
            f"Invalid VCD schema in '{schema_name}': 'tracepoints' must contain at least "
            f"'basic_layer' or 'derived_layer'."
        )


def validate_config(config: Any, config_name: str = "config.json"):
    """Validates that the configuration dictionary contains required keys."""
    if not isinstance(config, dict):
        raise ValueError(
            f"Invalid config in '{config_name}': expected a JSON object at root."
        )
    if "General" not in config or "TracePoints" not in config:
        raise ValueError(
            f"Invalid config in '{config_name}': missing mandatory 'General' or 'TracePoints' sections."
        )


# ==============================================================================
# 5. POLICY-DRIVEN VCD GENERATOR CORE
# ==============================================================================
@dataclass
class VCDVar:
    symbol: str
    identifier: str
    var_type: str
    init_val: Any


class PolicyVCDGenerator:

    def __init__(
        self,
        vcd_schema: Dict[str, Any],
        config: Dict[str, Any],
        trace_data: Union[Dict[str, Any], List[Dict[str, Any]]],
        schema_name: str = "vcd_schema",
    ):
        validate_vcd_schema(vcd_schema, schema_name)
        validate_config(config)

        self.schema = vcd_schema
        self.config = config

        if isinstance(trace_data, dict):
            self.trace_buffer: List[Dict[str, Any]] = trace_data.get("Tracepoints", [])
            general = trace_data.get("General", {})
        elif isinstance(trace_data, list):
            self.trace_buffer = trace_data
            general = {}
        else:
            raise ValueError(
                "Trace data must be a JSON object containing 'Tracepoints' or a JSON array of records."
            )

        if not self.trace_buffer:
            raise ValueError(
                "Trace buffer contains no tracepoint records. Cannot generate VCD file from empty trace."
            )

        # Resolve timescale from General.TimestampUnit
        ts_unit = str(general.get("TimestampUnit", "us")).strip()
        if not any(ts_unit.startswith(str(d)) for d in (1, 10, 100)):
            ts_unit = f"1{ts_unit}"
        self.timescale = ts_unit

        self.id_lookup = build_id_lookup(config)

        self.symbol_gen = VCDSymbolGenerator()
        self.variables: Dict[str, VCDVar] = {}
        self.trackers: Dict[Tuple[str, int], PolicyTracker] = {}

        self.tp_cfg: Dict[str, Any] = self.schema.get("tracepoints", {})
        self.basic_layer: Dict[str, Any] = self.tp_cfg.get("basic_layer", {})
        self.derived_layer: List[Dict[str, Any]] = self.tp_cfg.get(
            "derived_layer", []
        )

    def _render_template(
        self, template: str, context: Dict[str, Any]
    ) -> str:
        res = str(template)
        for k, v in context.items():
            res = res.replace(f"${{{k}}}", str(v))
        return res

    def _fallback_record_context(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback to extract context if ID lookup is not available."""
        tp_type = record.get("Type") or record.get("type", "")
        name_str = record.get("Name") or record.get("name", "")
        subtype = record.get("Subtype") or record.get("subtype", "")
        group = record.get("Group") or record.get("group", "")
        base_name = ""
        prefix = ""
        tp_name = ""

        if name_str:
            if tp_type and f"_{tp_type}_" in name_str:
                left, right = name_str.split(f"_{tp_type}_", 1)
                base_name = left
                if not subtype:
                    subtype = right
                parts = base_name.split("_")
                if len(parts) >= 3:
                    prefix = parts[0]
                    group = parts[1]
                    tp_name = "_".join(parts[2:])
                elif len(parts) == 2:
                    group = parts[0]
                    tp_name = parts[1]
                else:
                    tp_name = base_name
            else:
                parts = name_str.split("_")
                base_name = name_str
                if len(parts) >= 2:
                    group = parts[0]
                    tp_name = "_".join(parts[1:])
                else:
                    tp_name = name_str

        full_name = name_str if name_str else base_name
        return {
            "prefix": prefix,
            "group": group,
            "name": tp_name,
            "type": tp_type,
            "subtype": subtype,
            "full_name": full_name,
            "base_name": base_name,
        }

    def _get_record_context(self, record: Dict[str, Any]) -> Dict[str, Any]:
        rec_id = normalize_id(record.get("ID", ""))
        entry = self.id_lookup.get(rec_id)
        if entry:
            return build_context(entry)
        return self._fallback_record_context(record)

    def _find_derived_rules(
        self, record: Dict[str, Any], ctx: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        rec_type = ctx.get("type", "")
        if not rec_type:
            return []
        rules = []
        for entry in self.derived_layer:
            if entry.get("match_type") == rec_type:
                rules.extend(entry.get("derived_traces", []))
        return rules

    @staticmethod
    def _matches_subtype(
        record_subtype: str, target: Union[str, List[str], None]
    ) -> bool:
        if target is None:
            return False
        if isinstance(target, list):
            return record_subtype in target
        return record_subtype == target

    def _get_action(
        self, record_subtype: str, rule: Dict[str, Any]
    ) -> Optional[str]:
        if self._matches_subtype(record_subtype, rule.get("increase_on")):
            return "increase"
        if self._matches_subtype(record_subtype, rule.get("decrease_on")):
            return "decrease"
        if self._matches_subtype(record_subtype, rule.get("activate_on")):
            return "activate"
        if self._matches_subtype(record_subtype, rule.get("deactivate_on")):
            return "deactivate"
        return None

    def _flatten_record_data(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        if "Data" in record:
            return flatten_data(record.get("Data") or [])
        if "args" in record:
            raw_args = record.get("args") or []
            converted = []
            for a in raw_args:
                if isinstance(a, dict):
                    converted.append({
                        "Value": a.get("val", a.get("Value")),
                        "Stringify": a.get("str", a.get("Stringify")),
                    })
                else:
                    converted.append({"Value": a, "Stringify": None})
            return flatten_data(converted)
        return []

    def pass1_analyze_and_declare(self):
        """Pass 1: Calculate max slot usage and declare all Basic + Derived + Argument variables."""
        # A. Analyze policy actions across all records to determine max_depth_seen for multi-trace policies
        for record in self.trace_buffer:
            ctx = self._get_record_context(record)
            derived_rules = self._find_derived_rules(record, ctx)
            if not derived_rules:
                continue

            base_name = ctx["base_name"]
            subtype = ctx.get("subtype", "")

            for t_idx, rule in enumerate(derived_rules):
                tracker_key = (base_name, t_idx)
                if tracker_key not in self.trackers:
                    raw_init = rule.get("on_init")
                    if raw_init is None and "on_init" not in rule:
                        raw_init = rule.get("idle_vcd_value", "z")
                    self.trackers[tracker_key] = PolicyTracker(
                        policy=rule.get("policy", "multi_trace_fifo"),
                        idle_val=None if is_null_val(rule.get("idle_vcd_value")) else str(rule.get("idle_vcd_value", "z")),
                        active_val=None if is_null_val(rule.get("active_vcd_value")) else str(rule.get("active_vcd_value", "x")),
                        init_val=raw_init,
                    )

                action = self._get_action(subtype, rule)
                if action:
                    self.trackers[tracker_key].execute_action(action)

        # Reset active tracker states before Pass 2 execution
        for tracker in self.trackers.values():
            tracker.reset()

        # B. Declare VCD Variables
        args_schema = self.basic_layer.get("arguments", {})

        for record in self.trace_buffer:
            ctx = self._get_record_context(record)

            # --- BASIC LAYER: PRIMARY DECLARATION ---
            if "primary" in self.basic_layer:
                p_cfg = self.basic_layer["primary"]
                p_id = self._render_template(p_cfg["identifier"], ctx)
                if p_id not in self.variables:
                    self.variables[p_id] = VCDVar(
                        symbol=self.symbol_gen.next_symbol(),
                        identifier=p_id,
                        var_type=p_cfg.get("var_type", "event"),
                        init_val=p_cfg.get("on_init"),
                    )

            # --- BASIC LAYER: ARGUMENTS DECLARATION ---
            data_items = self._flatten_record_data(record)
            for item in data_items:
                arg_idx = item["arg_idx"]
                array_idx = item["array_idx"]
                val = item["val"]
                stringify = item["stringify"]

                cat_cfg = args_schema.get("scalar" if array_idx is None else "array", {})

                # Raw argument variable
                if "raw" in cat_cfg:
                    raw_cfg = cat_cfg["raw"]
                    ctx_arg = {
                        **ctx,
                        "arg_idx": arg_idx,
                        "array_idx": array_idx if array_idx is not None else 0,
                        "arg_val": val,
                    }
                    raw_id = self._render_template(raw_cfg["identifier"], ctx_arg)
                    if raw_id not in self.variables:
                        self.variables[raw_id] = VCDVar(
                            symbol=self.symbol_gen.next_symbol(),
                            identifier=raw_id,
                            var_type=raw_cfg.get("var_type", "real 32"),
                            init_val=raw_cfg.get("on_init", 0),
                        )

                # Stringified argument variable
                if "stringified" in cat_cfg and stringify is not None:
                    str_cfg = cat_cfg["stringified"]
                    ctx_arg = {
                        **ctx,
                        "arg_idx": arg_idx,
                        "array_idx": array_idx if array_idx is not None else 0,
                        "stringify_str": stringify,
                    }
                    str_id = self._render_template(str_cfg["identifier"], ctx_arg)
                    if str_id not in self.variables:
                        self.variables[str_id] = VCDVar(
                            symbol=self.symbol_gen.next_symbol(),
                            identifier=str_id,
                            var_type=str_cfg.get("var_type", "string"),
                            init_val=str_cfg.get("on_init", "_"),
                        )

            # --- DERIVED LAYER DECLARATIONS ---
            derived_rules = self._find_derived_rules(record, ctx)
            base_name = ctx["base_name"]

            for t_idx, rule in enumerate(derived_rules):
                tracker_key = (base_name, t_idx)
                if tracker_key in self.trackers:
                    tracker = self.trackers[tracker_key]
                    max_slots = max(1, tracker.max_depth_seen)

                    var_type_str = rule.get("var_type", "wire 1")
                    if rule.get("policy") == "counter":
                        var_type_str = check_counter_var_type(
                            var_type_str, rule.get("identifier", "")
                        )

                    # on_init support for derived layer (falls back to idle_vcd_value)
                    raw_init = rule.get("on_init")
                    if raw_init is None and "on_init" not in rule:
                        raw_init = rule.get("idle_vcd_value", "z")
                    init_val = None if is_null_val(raw_init) else raw_init

                    for slot_i in range(1, max_slots + 1):
                        slot_ctx = {**ctx, "slot_idx": slot_i}
                        slot_id = self._render_template(
                            rule["identifier"], slot_ctx
                        )
                        if slot_id not in self.variables:
                            self.variables[slot_id] = VCDVar(
                                symbol=self.symbol_gen.next_symbol(),
                                identifier=slot_id,
                                var_type=var_type_str,
                                init_val=init_val,
                            )

        # Check that at least one variable was declared
        if not self.variables:
            raise ValueError(
                "No VCD variables were declared. Check that the VCD schema definitions match the tracepoints in the trace. "
                "Refusing to generate a malformed empty VCD file."
            )

    def write_vcd_header(self) -> List[str]:
        today_str = date.today().isoformat()
        lines = [
            "$date",
            f"   {today_str}",
            "$end",
            "$version",
            "   IEEE 1364 Multi/Single Policy VCD Engine",
            "$end",
            f"$timescale {self.timescale} $end",
            "$scope module top $end",
        ]

        for var in self.variables.values():
            base_type, size = resolve_var_type_and_size(var.var_type)
            lines.append(
                f"$var {base_type} {size} {var.symbol} {var.identifier} $end"
            )

        lines.extend(["$upscope $end", "$enddefinitions $end", "$dumpvars"])

        for var in self.variables.values():
            if not is_null_val(var.init_val):
                formatted = format_vcd_value(var.init_val, var.symbol, var.var_type)
                if formatted is not None:
                    lines.append(formatted)

        lines.append("$end")
        return lines

    def pass2_generate_stream(self) -> str:
        self.pass1_analyze_and_declare()
        lines = self.write_vcd_header()

        current_ts = -1
        args_schema = self.basic_layer.get("arguments", {})

        for record in self.trace_buffer:
            ts = record.get("Timestamp", record.get("timestamp", 0))
            if ts != current_ts:
                lines.append(f"#{ts}")
                current_ts = ts

            ctx = self._get_record_context(record)

            # --- PROCESS BASIC LAYER: PRIMARY ---
            if "primary" in self.basic_layer:
                p_cfg = self.basic_layer["primary"]
                occ_val = p_cfg.get("on_occurrence", 1)
                if not is_null_val(occ_val):
                    p_id = self._render_template(p_cfg["identifier"], ctx)
                    if p_id in self.variables:
                        var = self.variables[p_id]
                        formatted = format_vcd_value(occ_val, var.symbol, var.var_type)
                        if formatted is not None:
                            lines.append(formatted)

            # --- PROCESS BASIC LAYER: ARGUMENTS ---
            data_items = self._flatten_record_data(record)
            for item in data_items:
                arg_idx = item["arg_idx"]
                array_idx = item["array_idx"]
                val = item["val"]
                stringify = item["stringify"]

                cat_cfg = args_schema.get("scalar" if array_idx is None else "array", {})

                if "raw" in cat_cfg and not is_null_val(val):
                    raw_cfg = cat_cfg["raw"]
                    ctx_arg = {
                        **ctx,
                        "arg_idx": arg_idx,
                        "array_idx": array_idx if array_idx is not None else 0,
                        "arg_val": val,
                    }
                    raw_id = self._render_template(raw_cfg["identifier"], ctx_arg)
                    if raw_id in self.variables:
                        var = self.variables[raw_id]
                        formatted = format_vcd_value(val, var.symbol, var.var_type)
                        if formatted is not None:
                            lines.append(formatted)

                if "stringified" in cat_cfg and stringify is not None and not is_null_val(stringify):
                    str_cfg = cat_cfg["stringified"]
                    ctx_arg = {
                        **ctx,
                        "arg_idx": arg_idx,
                        "array_idx": array_idx if array_idx is not None else 0,
                        "stringify_str": stringify,
                    }
                    str_id = self._render_template(str_cfg["identifier"], ctx_arg)
                    if str_id in self.variables:
                        var = self.variables[str_id]
                        formatted = format_vcd_value(stringify, var.symbol, var.var_type)
                        if formatted is not None:
                            lines.append(formatted)

            # --- PROCESS DERIVED LAYER ---
            derived_rules = self._find_derived_rules(record, ctx)
            base_name = ctx["base_name"]
            subtype = ctx.get("subtype", "")

            for t_idx, rule in enumerate(derived_rules):
                tracker_key = (base_name, t_idx)
                if tracker_key in self.trackers:
                    tracker = self.trackers[tracker_key]
                    action = self._get_action(subtype, rule)

                    if action:
                        res = tracker.execute_action(action)
                        if res:
                            slot_idx, new_vcd_state = res
                            if not is_null_val(new_vcd_state):
                                slot_ctx = {**ctx, "slot_idx": slot_idx}
                                slot_id = self._render_template(
                                    rule["identifier"], slot_ctx
                                )

                                if slot_id in self.variables:
                                    var = self.variables[slot_id]
                                    formatted = format_vcd_value(
                                        new_vcd_state,
                                        var.symbol,
                                        var.var_type,
                                    )
                                    if formatted is not None:
                                        lines.append(formatted)

        return "\n".join(lines)


# ==============================================================================
# 6. ENTRYPOINT
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Policy-Driven IEEE 1364 VCD Generator."
    )
    parser.add_argument(
        "--vcd-schema", required=True, help="Path to YAML VCD Schema"
    )
    parser.add_argument(
        "--config", "--tp-config", dest="config", required=True, help="Path to Tracepoint Config JSON"
    )
    parser.add_argument(
        "--trace-json", "--trace-buffer", dest="trace_json", required=True, help="Path to Trace Buffer JSON"
    )
    parser.add_argument(
        "--output", default="output.vcd", help="Output VCD path"
    )

    args = parser.parse_args()

    try:
        validate_inputs(args.vcd_schema, args.config, args.trace_json)

        with open(args.vcd_schema, "r", encoding="utf-8") as f:
            vcd_schema = yaml.safe_load(f)
        with open(args.config, "r", encoding="utf-8") as f:
            config = json.load(f)
        with open(args.trace_json, "r", encoding="utf-8") as f:
            trace_data = json.load(f)

        generator = PolicyVCDGenerator(
            vcd_schema, config, trace_data, schema_name=args.vcd_schema
        )
        vcd_data = generator.pass2_generate_stream()

        with open(args.output, "w", encoding="utf-8") as f:
            f.write(vcd_data)

        print(f"Generated VCD file: {args.output}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()