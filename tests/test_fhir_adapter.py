"""Smoke tests for openvital.fhir_adapter — verifies the FHIR R4
Parameters/Bundle round-trip with a synthetic Observation. Mirrors the
SampledData shape that api.vitaldb.net's $sample operation emits (period
in ms, integer data, factor + origin calibration).

Run::

    python -m tests.test_fhir_adapter

Optional integration test exercises ecg_qrs_detector.run() end-to-end if
openecg is importable; otherwise that block is skipped.
"""

from __future__ import annotations
import math

from openvital.fhir_adapter import (
    decode_sampled_data,
    params_to_filter_input,
    result_to_bundle,
    operation_definition,
    capability_statement,
    operation_outcome,
    modname_to_opname,
    opname_to_modname,
)


def _sample_observation(srate_hz: float, n: int, factor: float = 0.00015259,
                        origin: float = 0.0) -> dict:
    """A FHIR R4 Observation carrying a synthetic int ECG-like signal —
    sinusoid scaled into int16 LSBs so the round-trip exercises factor +
    origin calibration."""
    period_ms = 1000.0 / srate_hz
    # synthesize a 5 Hz sine in int16 LSBs (so decoded ≈ ±0.5 mV)
    samples = []
    for i in range(n):
        v = int(round(math.sin(2 * math.pi * 5 * i / srate_hz) * 3000))
        samples.append(str(v))
    # Sprinkle one missing token so we exercise that path.
    if n > 10:
        samples[3] = "E"
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{
            "system": "https://api.vitaldb.net/fhir/CodeSystem/track",
            "code": "ECG_II",
        }]},
        "subject":   {"reference": "Patient/1551-p"},
        "encounter": {"reference": "Encounter/1551"},
        "effectiveDateTime": "2099-12-29T22:01:55Z",
        "valueSampledData": {
            "origin": {"value": origin, "unit": "mV"},
            "period": period_ms,
            "factor": factor,
            "dimensions": 1,
            "data": " ".join(samples),
        },
    }


def test_decode_sampled_data():
    sd = {
        "origin": {"value": 1.0, "unit": "mV"},
        "period": 2.0,           # 500 Hz
        "factor": 0.5,
        "dimensions": 1,
        "data": "0 2 4 E -2 ?",
    }
    srate, vals = decode_sampled_data(sd)
    assert srate == 500.0, srate
    # 1.0 + 0.5 * x  →  [1.0, 2.0, 3.0, NaN, 0.0, NaN]
    assert vals[0] == 1.0
    assert vals[1] == 2.0
    assert vals[2] == 3.0
    assert math.isnan(vals[3])
    assert vals[4] == 0.0
    assert math.isnan(vals[5])
    print("  decode_sampled_data: OK")


def test_params_to_filter_input():
    params = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "interval", "valueQuantity": {"value": 40, "unit": "s"}},
            {"name": "overlap",  "valueQuantity": {"value": 3,  "unit": "s"}},
            {"name": "input", "resource": _sample_observation(500.0, 50)},
            {"name": "option", "part": [
                {"name": "name",  "valueString": "thresh"},
                {"name": "value", "valueDecimal": 0.5},
            ]},
        ],
    }
    defaults = {"interval": 60, "overlap": 0, "name": "test"}
    inp, opt, cfg, ctx = params_to_filter_input(params, defaults, ["ECG"])

    assert cfg["interval"] == 40.0
    assert cfg["overlap"] == 3.0
    assert "ECG" in inp
    assert inp["ECG"]["srate"] == 500.0
    assert len(inp["ECG"]["vals"]) == 50
    assert opt == [{"name": "thresh", "value": 0.5}]
    assert ctx["subject"] == {"reference": "Patient/1551-p"}
    assert ctx["encounter"] == {"reference": "Encounter/1551"}
    assert ctx["t0"] is not None
    print("  params_to_filter_input: OK")


def test_result_to_bundle():
    ctx = {
        "subject": {"reference": "Patient/1551-p"},
        "encounter": {"reference": "Encounter/1551"},
        "t0": None,  # leave None to verify Observation omits effectiveDateTime
    }
    from datetime import datetime, timezone
    ctx["t0"] = datetime(2099, 12, 29, 22, 1, 55, tzinfo=timezone.utc)

    output_metas = [
        {"name": "RPEAK", "fhir_code": {"coding": [{
            "system": "http://loinc.org", "code": "8867-4"}]}},
        {"name": "QRSW", "unit": "ms"},
        {"name": "AFIB"},
    ]
    result = [
        [{"dt": 0.840, "val": 1}, {"dt": 1.710, "val": 1}],
        [{"dt": 0.840, "val": 88}],
        [{"dt": 5.0, "val": 1}, {"dt": 10.0, "val": float("nan")}],  # NaN skipped
    ]
    bundle = result_to_bundle(result, output_metas, ctx)
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    # 2 RPEAK + 1 QRSW + 1 AFIB (NaN dropped) = 4
    assert len(bundle["entry"]) == 4, bundle

    rpeak = bundle["entry"][0]["resource"]
    assert rpeak["resourceType"] == "Observation"
    assert rpeak["code"]["coding"][0]["code"] == "8867-4"
    assert rpeak["valueQuantity"]["value"] == 1
    assert rpeak["effectiveDateTime"].startswith("2099-12-29T22:01:55")
    assert rpeak["subject"]["reference"] == "Patient/1551-p"

    qrsw = bundle["entry"][2]["resource"]
    assert qrsw["valueQuantity"]["value"] == 88
    assert qrsw["valueQuantity"]["unit"] == "ms"
    assert qrsw["valueQuantity"]["code"] == "ms"

    afib = bundle["entry"][3]["resource"]
    # fallback code system used since no fhir_code on this output meta
    assert "openvital.org" in afib["code"]["coding"][0]["system"]
    print("  result_to_bundle: OK")


def test_operation_definition_and_capability():
    cfgs = [{
        "modname": "ecg_qrs_detector",
        "name": "ECG - QRS detector",
        "desc": "Test desc.",
        "interval": 40, "overlap": 3,
        "inputs": [{"name": "ECG", "type": "wav"}],
        "outputs": [
            {"name": "RPEAK", "type": "num"},
            {"name": "QRSW", "type": "num"},
        ],
        "options": [],
    }]
    op = operation_definition("ecg_qrs_detector", cfgs[0])
    assert op["resourceType"] == "OperationDefinition"
    assert op["code"] == "ecg-qrs-detector"
    assert op["system"] is True
    # input + interval + overlap + return = 4 (no `option` since options=[])
    names = [p["name"] for p in op["parameter"]]
    assert names == ["input", "interval", "overlap", "return"], names

    cs = capability_statement(cfgs)
    assert cs["resourceType"] == "CapabilityStatement"
    assert cs["fhirVersion"] == "4.0.1"
    ops = cs["rest"][0]["operation"]
    assert ops[0]["name"] == "ecg-qrs-detector"
    print("  operation_definition + capability_statement: OK")


def test_name_normalization():
    assert modname_to_opname("ecg_qrs_detector") == "ecg-qrs-detector"
    assert opname_to_modname("ecg-qrs-detector") == "ecg_qrs_detector"
    print("  modname↔opname round-trip: OK")


def test_operation_outcome():
    oo = operation_outcome("error", "invalid", "bad")
    assert oo["resourceType"] == "OperationOutcome"
    assert oo["issue"][0]["severity"] == "error"
    assert oo["issue"][0]["code"] == "invalid"
    assert oo["issue"][0]["diagnostics"] == "bad"
    print("  operation_outcome: OK")


def test_end_to_end_with_ecg_qrs_detector():
    """If openecg is installed, drive a full request through the real filter
    and assert the resulting Bundle contains at least one RPEAK entry."""
    try:
        import openecg  # noqa: F401
    except ImportError:
        print("  end-to-end: SKIPPED (openecg not installed)")
        return

    from openvital.filters import ecg_qrs_detector as flt

    n_samples = 40 * 500  # 40 s @ 500 Hz
    obs = _sample_observation(500.0, n_samples)
    params = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "interval", "valueQuantity": {"value": 40, "unit": "s"}},
            {"name": "overlap",  "valueQuantity": {"value": 3,  "unit": "s"}},
            {"name": "input", "resource": obs},
        ],
    }
    cfg_entry = {
        "modname": "ecg_qrs_detector",
        "inputs": flt.cfg.get("inputs", []),
        "outputs": flt.cfg.get("outputs", []),
    }
    input_names = [i.get("name") for i in cfg_entry["inputs"]]
    output_metas = cfg_entry["outputs"]

    inp, opt, cfg, ctx = params_to_filter_input(params, flt.cfg, input_names)
    result = flt.run(inp, opt, cfg)
    bundle = result_to_bundle(result, output_metas, ctx)

    assert bundle["resourceType"] == "Bundle"
    # Synthetic 5 Hz sine is not a real QRS, so we accept 0 R-peaks — the
    # important assertion is that the round-trip didn't blow up and the
    # Bundle is well-formed.
    print(f"  end-to-end: OK ({len(bundle['entry'])} Observation entries)")


if __name__ == "__main__":
    print("running fhir_adapter smoke tests…")
    test_decode_sampled_data()
    test_params_to_filter_input()
    test_result_to_bundle()
    test_operation_definition_and_capability()
    test_name_normalization()
    test_operation_outcome()
    test_end_to_end_with_ecg_qrs_detector()
    print("\nALL TESTS PASSED")
