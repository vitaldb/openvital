# Running openvital filter server on AWS Lambda

This document records the design choices and operational notes for the
Lambda deployment of `python -m openvital`. Update this file whenever
behavior that affects the Lambda container changes.

## Why Lambda

The filter server is a stateless HTTP front for `openvital/filters/*.py`
modules. Average traffic is very low (idle most of the day, brief bursts
when VR users open the filter dialog). Always-on EC2 is over-provisioned
by orders of magnitude. Lambda + container image gives:

- ~$1/mo at observed traffic vs ~$45/mo on EC2 (t3a.medium + ALB + EBS).
- Auto-scale on demand, zero cost when idle.
- Memory leak fix (the prior `cfgs[invokeid]` dict on EC2 grew unbounded).

## Wire protocols

The Lambda exposes two wire formats on the same container, dispatched by
URL prefix in `openvital/__main__.py`:

### Legacy JSON (VR — UNCHANGED)

- `GET /` → JSON array of filter metadata (modname, name, group, desc,
  interval, overlap, inputs, outputs). Each entry is what the module's
  `cfg` dict declares plus inputs/outputs lists. Clients are expected to
  read only these well-known keys.
- `POST /<modname>` → request body is gzipped JSON
  `{interval, overlap, invokeid, inputs, options}`; response is gzipped
  JSON list-of-tracks (`[[{dt,val}, ...], ...]`) or `null`.

If you change any of these field names or shapes, update **all three** in
the same release: `openvital/__main__.py`, the deployed server (Lambda
container or fallback EC2), and the VR client (`src/FILT.cpp`,
`src/VRApp.cpp::CVRApp::load_filters`).

### FHIR R4 (public, since 0.4.0a1)

Each filter is also a system-level FHIR Operation, matching the encoding
that `api.vitaldb.net/fhir` emits.

- `GET /fhir/metadata` → `CapabilityStatement` listing every filter as an
  operation.
- `GET /fhir/OperationDefinition/<filter-name>` → input/output contract,
  derived live from the filter module's `cfg` dict (so adding a new filter
  module is enough — no spec edit).
- `POST /fhir/$<filter-name>` → `Parameters` in (one `Observation` per
  input slot, plus `interval`/`overlap`/`option`), `Bundle` of result
  `Observation`s out. SampledData is decoded with `factor`/`origin`
  applied; `E`/`L`/`U`/`?` tokens become NaN. Filter names use hyphens
  (`ecg-qrs-detector`) where module names use underscores.

The FHIR adapter lives in `openvital/fhir_adapter.py` (pure functions,
no I/O — Lambda-safe). The handler in `__main__.py` is a thin router and
response builder around it.

## What changed in `__main__.py` for Lambda

Originally the server kept a global `cfgs[invokeid]` dict so a filter
could accumulate state across consecutive POSTs from the same VR run
(only `pleth_pvi.py` actually used this — for an EMA-style smoothing of
pulse-pressure across breaths). On Lambda, requests can land on different
container instances at any time, so per-invokeid affinity is not
guaranteed.

The handler is now stateless: every POST starts with a deep-copy of
`default_cfgs[modname]` (the module's static `cfg` dict). Concretely:

- `cfgs = {}` global removed.
- `if invokeid not in cfgs: ...` block replaced with a
  `cfg = copy.deepcopy(default_cfgs[modname])` per call.
- Comment block above `FilterHandler` updated.

**Implication for filter authors:** if your filter's `run(inp, opt, cfg)`
mutates `cfg` between calls expecting persistence (the way pleth_pvi did
with `cfg['pp']`), that persistence is gone. Either:

1. Accept it: design the filter to converge within a single call (a
   60+ second window is usually enough for EMA-style smoothing).
2. Externalize the state to DynamoDB keyed on `invokeid` (~$0.5/mo,
   adds complexity). No filter currently needs this.

## Build & deploy

### Local test

```bash
docker build -t openvital-filter:poc .
docker run --rm -p 3001:3000 openvital-filter:poc
curl -s --compressed http://localhost:3001/ | python -m json.tool | head
```

The image uses [aws-lambda-web-adapter](https://github.com/awslabs/aws-lambda-web-adapter)
so the same `python -m openvital` entrypoint works locally and in
Lambda. `AWS_LWA_PORT=3000` tells the adapter which port the app binds.

### ECR + Lambda

```bash
# 1. Login + push (requires ecr:* + lambda:* + iam:PassRole on the Lambda exec role)
aws ecr get-login-password --region ap-northeast-2 | docker login --username AWS \
    --password-stdin 595007890878.dkr.ecr.ap-northeast-2.amazonaws.com
docker tag openvital-filter:poc 595007890878.dkr.ecr.ap-northeast-2.amazonaws.com/openvital-filter:latest
docker push 595007890878.dkr.ecr.ap-northeast-2.amazonaws.com/openvital-filter:latest

# 2. Update the Lambda function (creation steps documented in the
#    VitalRecorder repo's project memory).
aws lambda update-function-code \
    --function-name openvital-filter \
    --image-uri 595007890878.dkr.ecr.ap-northeast-2.amazonaws.com/openvital-filter:latest
```

### Update procedure

When a PR changes anything in `openvital/`:

1. Confirm `python -m openvital` still imports and serves `GET /`
   correctly with `docker build` + `docker run` locally.
2. Push a new image tag and run `lambda update-function-code` (or do it
   via your CI pipeline).
3. Hit `https://filter.vitaldb.net/` to verify the live endpoint and
   ensure the new filter list is what you expect.
4. If the wire format changed, coordinate with the VR client release —
   see the "Wire protocol — UNCHANGED" section above.

## Cold-start behavior

Base image (numpy + scipy + openecg + openvital) imports in 1–2s. ML
extras (`tensorflow`, `torch`) push that to 8–15s and are not part of the
default container. If the ML filters need to run on Lambda too, build a
separate image with `pip install openvital[ml-tf,ml-torch,signal]` and
mount it under a different Lambda function (or use Provisioned
Concurrency for the latency-sensitive case).

## Resources

- Filter EC2 (legacy fallback): `i-083c784bf47dc9e2f` in
  `595007890878:ap-northeast-2`. Slated for termination once Lambda is
  verified.
- ECR repo: `595007890878.dkr.ecr.ap-northeast-2.amazonaws.com/openvital-filter`.
- Lambda function name: `openvital-filter` (container image,
  ap-northeast-2). The Lambda Function URL is `AWS_IAM`-auth so the
  public face is always API Gateway, never the Function URL directly.
- HTTP API v2 `openvital-filter` (id `rajpvo0wd1`, ap-northeast-2) — its
  `$default` stage routes to the Lambda. Both custom domains map to this
  one stage:
  - `filter.vitaldb.net` (VR) — REGIONAL custom domain, ACM cert in
    ap-northeast-2.
  - `api.openvital.io` (public) — REGIONAL custom domain, separate ACM
    cert in ap-northeast-2.
- For the `api.openvital.io` wiring (Route 53 + ACM + APIGW custom
  domain + API mapping + A-alias), see [`OPENVITAL_IO.md`](OPENVITAL_IO.md)
  and the idempotent script `scripts/setup_openvital_io.sh`.
