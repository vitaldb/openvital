"""FHIR R4 Operation adapter for OpenVital filters.

Translates between FHIR ``Parameters`` / ``Observation`` / ``Bundle`` resources
and the internal ``(inp, opt, cfg)`` + track-list contract used by filter
modules. Pure functions, no I/O — Lambda-safe.

Endpoint convention (system-level Operations):

    POST /fhir/$<filter-name>      ← Parameters in, Bundle out
    GET  /fhir/OperationDefinition/<filter-name>
    GET  /fhir/metadata             (CapabilityStatement listing all filters)

SampledData encoding follows what ``api.vitaldb.net/fhir`` emits: FHIR R4
4.0.1 with ``period`` (ms) for sampling interval, ``factor`` for gain,
``origin`` for bias, and space-separated integer/decimal tokens in ``data``.
Special tokens ``E`` (error) / ``L`` (under) / ``U`` (over) / ``?`` (missing)
decode to NaN. Filter modules see the same ``(srate, vals)`` shape they
always have — calibration is unwound here at the boundary.
"""

from __future__ import annotations
import copy
import math
from datetime import datetime, timedelta, timezone


# Filter modules are snake_case; FHIR operation codes are lowercase-hyphen.
def modname_to_opname(modname: str) -> str:
    return modname.replace("_", "-")


def opname_to_modname(opname: str) -> str:
    return opname.replace("-", "_")


# ---- Input adapter ---------------------------------------------------------

_MISSING_TOKENS = {"E", "L", "U", "?"}


def decode_sampled_data(sd: dict) -> tuple[float, list[float]]:
    """Decode a FHIR R4 SampledData → (srate_hz, calibrated_vals).

    Applies ``factor`` (gain) and ``origin.value`` (bias) so the filter sees
    physical units. Missing/saturation tokens become NaN — filters use
    ``openvital.interp_undefined`` to fill them.
    """
    if "period" not in sd:
        raise ValueError("SampledData.period is required (ms between samples)")
    period_ms = float(sd["period"])
    if period_ms <= 0:
        raise ValueError(f"SampledData.period must be > 0, got {period_ms}")
    srate = 1000.0 / period_ms
    factor = float(sd.get("factor", 1.0))
    origin_val = float(sd.get("origin", {}).get("value", 0.0))

    nan = float("nan")
    vals: list[float] = []
    for tok in sd.get("data", "").split():
        if tok in _MISSING_TOKENS:
            vals.append(nan)
            continue
        try:
            vals.append(origin_val + factor * float(tok))
        except ValueError:
            vals.append(nan)
    return srate, vals


def _parse_iso8601(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _observation_start(obs: dict) -> datetime | None:
    if "effectiveDateTime" in obs:
        return _parse_iso8601(obs["effectiveDateTime"])
    if "effectivePeriod" in obs:
        start = obs["effectivePeriod"].get("start")
        if start:
            return _parse_iso8601(start)
    return None


def params_to_filter_input(
    params: dict,
    defaults_cfg: dict,
    filter_input_names: list,
) -> tuple[dict, list, dict, dict]:
    """``Parameters`` → ``(inp, opt, cfg, ctx)``.

    ``filter_input_names`` is ``[i['name'] for i in cfg['inputs']]`` — the
    names the filter's ``run()`` expects as keys of ``inp``. Input
    Observations are matched positionally to that list (first ``input`` →
    first slot, etc.). Extra inputs are ignored; missing inputs raise.

    ``ctx`` carries Patient/Encounter references and the window-start
    datetime so ``result_to_bundle`` can stamp output Observations.
    """
    if params.get("resourceType") != "Parameters":
        raise ValueError(
            f"expected Parameters resource, got {params.get('resourceType')!r}"
        )

    cfg = copy.deepcopy(defaults_cfg)
    inp: dict = {}
    opt: list = []
    ctx: dict = {"subject": None, "encounter": None, "t0": None}

    input_obs: list = []

    for p in params.get("parameter", []):
        name = p.get("name")
        if name == "interval":
            v = p.get("valueQuantity", {}).get("value")
            if v is not None:
                cfg["interval"] = float(v)
        elif name == "overlap":
            v = p.get("valueQuantity", {}).get("value")
            if v is not None:
                cfg["overlap"] = float(v)
        elif name == "invokeid":
            cfg["invokeid"] = p.get("valueString", "")
        elif name == "input":
            res = p.get("resource", {})
            if res.get("resourceType") == "Observation":
                input_obs.append(res)
        elif name == "option":
            kv: dict = {}
            for sub in p.get("part", []):
                k = sub.get("name")
                if k is None:
                    continue
                for key in ("valueString", "valueDecimal", "valueInteger",
                           "valueBoolean"):
                    if key in sub:
                        kv[k] = sub[key]
                        break
            if "name" in kv:
                opt.append(kv)

    if not input_obs:
        raise ValueError("at least one 'input' Observation parameter is required")

    for idx, obs in enumerate(input_obs):
        if idx >= len(filter_input_names):
            break
        slot = filter_input_names[idx]
        sd = obs.get("valueSampledData")
        if not sd:
            raise ValueError(
                f"input #{idx} ({slot!r}): Observation must carry valueSampledData"
            )
        srate, vals = decode_sampled_data(sd)
        inp[slot] = {"srate": srate, "vals": vals}
        if idx == 0:
            ctx["subject"] = obs.get("subject")
            ctx["encounter"] = obs.get("encounter")
            ctx["t0"] = _observation_start(obs)

    if "invokeid" not in cfg:
        cfg["invokeid"] = f"fhir-{id(params):x}"

    return inp, opt, cfg, ctx


# ---- Output adapter --------------------------------------------------------

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return s[:-3] + "Z"


def _output_observation(meta: dict, val, dt_offset: float, ctx: dict) -> dict:
    code = meta.get("fhir_code") or {
        "coding": [{
            "system": "https://openvital.org/fhir/CodeSystem/filter-output",
            "code": meta.get("name", "RESULT"),
            "display": meta.get("display", meta.get("name", "RESULT")),
        }],
    }
    obs: dict = {
        "resourceType": "Observation",
        "status": "final",
        "code": code,
        "valueQuantity": {"value": val},
    }
    if ctx.get("subject"):
        obs["subject"] = ctx["subject"]
    if ctx.get("encounter"):
        obs["encounter"] = ctx["encounter"]
    if ctx.get("t0"):
        obs["effectiveDateTime"] = _iso(ctx["t0"] + timedelta(seconds=dt_offset))
    unit = meta.get("unit")
    if unit:
        obs["valueQuantity"]["unit"] = unit
        obs["valueQuantity"]["system"] = "http://unitsofmeasure.org"
        obs["valueQuantity"]["code"] = unit
    return obs


def result_to_bundle(
    result_tracks,
    output_metas: list,
    ctx: dict,
) -> dict:
    """run() return ``[[{dt,val},...], ...]`` → FHIR ``Bundle`` of
    ``Observation``. NaN values are skipped (the filter's way of signalling
    'no decision').
    """
    entries: list = []
    for trk_idx, samples in enumerate(result_tracks or []):
        if trk_idx >= len(output_metas):
            break
        meta = output_metas[trk_idx]
        for s in samples or []:
            dt = s.get("dt")
            val = s.get("val")
            if dt is None or val is None:
                continue
            if isinstance(val, float) and math.isnan(val):
                continue
            entries.append({"resource": _output_observation(meta, val, dt, ctx)})

    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


# ---- Discovery -------------------------------------------------------------

def operation_definition(modname: str, cfg: dict) -> dict:
    op_name = modname_to_opname(modname)
    n_inputs = max(len(cfg.get("inputs", [])), 1)
    params: list = [
        {"name": "input", "use": "in", "min": 1, "max": str(n_inputs),
         "type": "Observation",
         "documentation": "Input waveform Observation(s) with valueSampledData. "
                          "Positional — first input maps to the first cfg.inputs slot."},
        {"name": "interval", "use": "in", "min": 0, "max": "1", "type": "Quantity",
         "documentation": f"Window length in seconds. Default {cfg.get('interval', 60)}."},
        {"name": "overlap", "use": "in", "min": 0, "max": "1", "type": "Quantity",
         "documentation": f"Window overlap in seconds. Default {cfg.get('overlap', 0)}."},
    ]
    if cfg.get("options"):
        params.append({
            "name": "option", "use": "in", "min": 0, "max": "*",
            "documentation": "Filter-specific option as name/value pair.",
            "part": [
                {"name": "name", "use": "in", "min": 1, "max": "1", "type": "string"},
                {"name": "value", "use": "in", "min": 1, "max": "1", "type": "string"},
            ],
        })
    params.append({
        "name": "return", "use": "out", "min": 1, "max": "1", "type": "Bundle",
        "documentation": "collection Bundle of result Observations.",
    })

    return {
        "resourceType": "OperationDefinition",
        "id": op_name,
        "name": op_name,
        "title": cfg.get("name", op_name),
        "status": "active",
        "kind": "operation",
        "code": op_name,
        "system": True, "type": False, "instance": False,
        "description": cfg.get("desc", ""),
        "parameter": params,
    }


def capability_statement(mod_cfgs: list) -> dict:
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "kind": "instance",
        "fhirVersion": "4.0.1",
        "format": ["json"],
        "implementation": {
            "description": "OpenVital filter operations",
            "url": "/fhir",
        },
        "rest": [{
            "mode": "server",
            "operation": [
                {
                    "name": modname_to_opname(c["modname"]),
                    "definition": "OperationDefinition/"
                                  + modname_to_opname(c["modname"]),
                }
                for c in mod_cfgs
            ],
        }],
    }


def operation_outcome(severity: str, code: str, msg: str) -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": severity, "code": code, "diagnostics": msg}],
    }
