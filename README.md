# openvital

Open-source Python library for biosignal analysis. Provides signal processing utilities and clinical algorithm implementations for ECG, arterial blood pressure (ABP), photoplethysmography (PPG), capnography (CO₂), EEG, NIRS, and related vital-signs waveforms.

`openvital` is the successor to [pyvital](https://github.com/vitaldb/pyvital) — same authors, broader scope. Where pyvital focused on ECG, openvital expands to cover the full multi-signal panel of a clinical patient monitor.

QRS detection delegates to [`openecg`](https://pypi.org/project/openecg/) (gradient-thresholded detector validated at micro-F1 = 0.994 on MIT-BIH Arrhythmia DB).

## Installation

```bash
pip install openvital
```

## Migrating from pyvital

The package and module name changed from `pyvital` to `openvital`; the API is otherwise unchanged.

```python
# before
import pyvital
r_peaks = pyvital.detect_qrs(ecg, srate=500)

# after
import openvital
r_peaks = openvital.detect_qrs(ecg, srate=500)
```

## Core Functions

```python
import openvital

# Interpolate NaN values
data = openvital.interp_undefined(raw_data)

# QRS detection (gradient-thresholded, via openecg)
r_peaks = openvital.detect_qrs(ecg_data, srate=500)

# Blood pressure / pleth peak detection
minlist, maxlist = openvital.detect_peaks(abp_data, srate=100)

# Bandpass filter
filtered = openvital.band_pass(data, srate=500, fl=5, fh=15)

# Resampling
resampled = openvital.resample_hz(data, srate_from=500, srate_to=100)
```

## Filters

Each filter module implements a `run(inp, opt, cfg)` function and a `cfg` dict describing its inputs, outputs, and parameters.

| Module | Description |
|--------|-------------|
| `abp_hpi` | Hypotension Prediction Index from arterial blood pressure |
| `abp_ppv` | Pulse Pressure Variation from arterial blood pressure |
| `ecg_annotator` | ECG waveform annotation using wavelets |
| `ecg_beat_noise_detector` | Beat/noise classification using deep learning |
| `ecg_classifier` | ECG rhythm and beat classification |
| `ecg_hrv` | Heart Rate Variability analysis |
| `ecg_mtwa` | Microvolt T-Wave Alternans detection |
| `ecg_qrs_detector` | R-peak detection (delegates to `openecg.detect_qrs`) |
| `eeg_fft` | EEG frequency analysis (band powers, SEF, MF) |
| `nirs_cox` | Cerebral oximetry autoregulation index (COx) |
| `pkpd_3comp` | Pharmacokinetic 3-compartment model |
| `pleth_dpop` | Delta POP from plethysmography |
| `pleth_ptt` | Pulse Transit Time |
| `pleth_pvi` | Pleth Variability Index |
| `pleth_spi` | Surgical Pleth Index |
| `resp_compliance` | Respiratory compliance |
| `sv_dlapco` | Stroke volume estimation (DLAPCO) |

## Filter Server

openvital includes a built-in HTTP server (Sanic) that exposes filters as REST endpoints:

```bash
python -m openvital [filter_folder] [port]
```

- `GET /` returns the list of available filters and their configurations.
- `POST /<module_name>` runs a filter with gzip-compressed JSON input.

## License

MIT
