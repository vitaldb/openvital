import openvital as arr

cfg = {
    'name': 'ECG - QRS detector',
    'group': 'Medical algorithms',
    'desc': 'Gradient-thresholded QRS detector with per-beat QRS width and '
            'rule-based AFib classification over rolling 10-s windows.',
    'reference': 'https://github.com/snu-bdac/openecg',
    'overlap': 3,  # 3 sec overlap for HR=20
    'interval': 40,
    'inputs': [{
        "name": 'ECG', "type": 'wav',
        "fhir_code": {"coding": [{
            "system": "urn:iso:std:iso:11073:10101",
            "code": "131329", "display": "ECG lead II"
        }]},
    }],
    'outputs': [
        {"name": 'RPEAK', "type": 'num', "min": 0, "max": 2,
         "fhir_code": {"coding": [{
             "system": "http://loinc.org",
             "code": "8867-4", "display": "Heart beat"
         }]}},
        {"name": 'QRSW', "type": 'num', "min": 40, "max": 220, "unit": "ms",
         "fhir_code": {"coding": [{
             "system": "http://loinc.org",
             "code": "LP30605-7", "display": "QRS duration"
         }]}},
        {"name": 'AFIB', "type": 'num', "min": 0, "max": 1,
         "fhir_code": {"coding": [{
             "system": "http://loinc.org",
             "code": "LA17075-2", "display": "Atrial fibrillation present"
         }]}},
    ]
}


# AFib decision uses 10-second sliding windows over the detected R-peaks.
# Slides every 5 s so each window decision lands at its mid-point.
_AFIB_WIN_S = 10.0
_AFIB_HOP_S = 5.0


def run(inp, opt, cfg):
    trk_name = [k for k in inp][0]

    if 'srate' not in inp[trk_name]:
        return

    data = arr.interp_undefined(inp[trk_name]['vals'])
    srate = inp[trk_name]['srate']

    # Detect R-peaks + per-beat QRS widths in one pass.
    r_list, w_list = arr.detect_qrs(data, srate, return_widths=True)

    ret_rpeak = []
    ret_qrsw = []
    for idx, w_ms in zip(r_list, w_list):
        dt = idx / srate
        ret_rpeak.append({'dt': dt, 'val': 1})
        ret_qrsw.append({'dt': dt, 'val': float(w_ms)})

    # Rolling-window AFib decision. Each window emits a {dt, val} sample
    # at the window mid-point so downstream readers can plot it as a
    # discrete track. A window with < 5 R-peaks is left undecided.
    from openecg import is_afib as _is_afib
    ret_afib = []
    n_total = len(data)
    win_n = int(round(_AFIB_WIN_S * srate))
    hop_n = int(round(_AFIB_HOP_S * srate))
    for start in range(0, max(1, n_total - win_n + 1), hop_n):
        end = start + win_n
        if end > n_total:
            break
        seg = data[start:end]
        try:
            val = 1 if _is_afib(seg, srate) else 0
        except Exception:
            continue
        mid_dt = (start + win_n / 2.0) / srate
        ret_afib.append({'dt': mid_dt, 'val': val})

    return [ret_rpeak, ret_qrsw, ret_afib]
