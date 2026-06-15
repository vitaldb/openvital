#!/usr/bin/env bash
#
# Build openvital container image, push to ECR, and update the
# openvital-filter Lambda function. Designed to run from WSL/Linux/macOS
# (Git Bash on Windows hits path-translation bugs — use WSL).
#
# Usage:
#   ./scripts/deploy_lambda.sh                       # version from pyproject.toml
#   ./scripts/deploy_lambda.sh v0.3.2                # explicit version tag
#   ./scripts/deploy_lambda.sh --skip-git-check      # don't fail on dirty tree
#   ./scripts/deploy_lambda.sh --skip-test           # don't curl after deploy
#
# Required: docker, aws CLI configured (--profile lucid-claude-code), python3.
#
set -euo pipefail

PROFILE=${PROFILE-lucid-claude-code}   # set PROFILE= (empty) to use the default chain
REGION=ap-northeast-2
ACCT=595007890878
ECR=$ACCT.dkr.ecr.$REGION.amazonaws.com
REPO=openvital-filter
FUNCTION=openvital-filter
LIVE_URL=https://filter.vitaldb.net/

# PROFILE may be empty (e.g. when running with env-var credentials only). In
# that case fall through to the default credential chain instead of passing
# an invalid `--profile` flag.
if [ -n "$PROFILE" ]; then
  PROFILE_ARGS=(--profile "$PROFILE")
else
  PROFILE_ARGS=()
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# --- args ---
SKIP_GIT=0
SKIP_TEST=0
EXPLICIT_VERSION=""
for arg in "$@"; do
  case "$arg" in
    --skip-git-check) SKIP_GIT=1 ;;
    --skip-test)      SKIP_TEST=1 ;;
    --help|-h)        sed -n '2,15p' "$0"; exit 0 ;;
    -*)               echo "unknown flag: $arg" >&2; exit 2 ;;
    *)                EXPLICIT_VERSION="$arg" ;;
  esac
done

# --- version ---
if [ -n "$EXPLICIT_VERSION" ]; then
  VERSION="${EXPLICIT_VERSION#v}"
else
  VERSION=$(python3 -c "
import re, sys
with open('pyproject.toml') as f:
    for line in f:
        m = re.match(r'^version\s*=\s*\"(.+)\"', line)
        if m: print(m.group(1)); sys.exit(0)
sys.exit('version not found in pyproject.toml')
")
fi
TAG=v$VERSION
IMAGE_LOCAL="$REPO:$TAG"
IMAGE_REMOTE="$ECR/$REPO:$TAG"
IMAGE_LATEST="$ECR/$REPO:latest"

echo "=== openvital-filter Lambda deploy ==="
echo "  version:  $VERSION  (tag $TAG)"
echo "  region:   $REGION"
echo "  function: $FUNCTION"
echo

# --- git sanity ---
if [ "$SKIP_GIT" -eq 0 ]; then
  if [ -n "$(git status --porcelain)" ]; then
    echo "✗ working tree is dirty. commit/stash first, or pass --skip-git-check." >&2
    git status --short >&2
    exit 1
  fi
  if ! git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "  (note: git tag '$TAG' does not exist — consider tagging this release)"
  fi
fi

# --- ECR login ---
echo "=== ECR login ==="
aws ecr get-login-password "${PROFILE_ARGS[@]}" --region "$REGION" \
  | docker login --username AWS --password-stdin "$ECR" >/dev/null
echo "  logged in to $ECR"

# --- build + push ---
echo
echo "=== build + push linux/amd64 ==="
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  -t "$IMAGE_REMOTE" \
  -t "$IMAGE_LATEST" \
  --push \
  .

# Resolve image digest from ECR for the deploy record.
DIGEST=$(aws ecr describe-images \
  "${PROFILE_ARGS[@]}" --region "$REGION" \
  --repository-name "$REPO" \
  --image-ids imageTag="$TAG" \
  --query 'imageDetails[0].imageDigest' --output text)
echo "  digest: $DIGEST"

# --- update Lambda ---
echo
echo "=== update Lambda function code ==="
aws lambda update-function-code \
  "${PROFILE_ARGS[@]}" --region "$REGION" \
  --function-name "$FUNCTION" \
  --image-uri "$IMAGE_REMOTE" \
  --query '[FunctionArn,LastUpdateStatus]' --output text

# Wait for the function to finish updating (image swap takes ~10-30s).
echo "  waiting for LastUpdateStatus=Successful ..."
for i in $(seq 1 60); do
  status=$(aws lambda get-function-configuration \
    "${PROFILE_ARGS[@]}" --region "$REGION" \
    --function-name "$FUNCTION" \
    --query 'LastUpdateStatus' --output text)
  if [ "$status" = "Successful" ]; then
    echo "  ✓ updated"
    break
  fi
  if [ "$status" = "Failed" ]; then
    reason=$(aws lambda get-function-configuration \
      "${PROFILE_ARGS[@]}" --region "$REGION" \
      --function-name "$FUNCTION" \
      --query 'LastUpdateStatusReason' --output text)
    echo "  ✗ update failed: $reason" >&2
    exit 1
  fi
  sleep 2
done

# --- live test ---
if [ "$SKIP_TEST" -eq 0 ]; then
  echo
  echo "=== live test: legacy $LIVE_URL ==="
  for attempt in 1 2 3; do
    if python3 -c "
import urllib.request, json, gzip, sys
req = urllib.request.Request('$LIVE_URL', headers={'Accept-Encoding': 'gzip'})
with urllib.request.urlopen(req, timeout=30) as r:
    body = r.read()
    if r.headers.get('Content-Encoding') == 'gzip':
        body = gzip.decompress(body)
    d = json.loads(body)
    print(f'  attempt $attempt: {len(d)} filters, status {r.status}')
    sys.exit(0 if r.status == 200 and len(d) > 0 else 1)
" ; then
      break
    fi
    echo "  attempt $attempt failed; retrying in 3s ..."
    sleep 3
  done

  echo
  echo "=== live test: FHIR ${LIVE_URL}fhir/metadata ==="
  python3 - <<PYEOF
import urllib.request, urllib.error, json, math, sys

BASE = "${LIVE_URL%/}"

def fail(msg):
    print(f"  ✗ {msg}", file=sys.stderr)
    sys.exit(1)

# 1) CapabilityStatement
try:
    with urllib.request.urlopen(BASE + "/fhir/metadata", timeout=30) as r:
        cap = json.loads(r.read())
except urllib.error.HTTPError as e:
    fail(f"GET /fhir/metadata HTTP {e.code}")
if cap.get("resourceType") != "CapabilityStatement":
    fail(f"unexpected resourceType {cap.get('resourceType')!r}")
ops = [o["name"] for o in cap["rest"][0]["operation"]]
print(f"  CapabilityStatement: fhirVersion={cap.get('fhirVersion')}  ops={len(ops)}")
if "ecg-qrs-detector" not in ops:
    fail("ecg-qrs-detector not advertised")

# 2) OperationDefinition
with urllib.request.urlopen(
    BASE + "/fhir/OperationDefinition/ecg-qrs-detector", timeout=30
) as r:
    opdef = json.loads(r.read())
if opdef.get("code") != "ecg-qrs-detector":
    fail(f"OperationDefinition.code={opdef.get('code')!r}")
print(f"  OperationDefinition: params={[p['name'] for p in opdef['parameter']]}")

# 3) POST /fhir/\$ecg-qrs-detector with a synthetic 40s 500Hz sine in int16 LSB.
srate = 500
n = 40 * srate
data = " ".join(
    str(int(round(math.sin(2 * math.pi * 5 * i / srate) * 3000)))
    for i in range(n)
)
params = {
    "resourceType": "Parameters",
    "parameter": [
        {"name": "interval", "valueQuantity": {"value": 40, "unit": "s"}},
        {"name": "overlap",  "valueQuantity": {"value": 3,  "unit": "s"}},
        {"name": "input", "resource": {
            "resourceType": "Observation",
            "status": "final",
            "code": {"coding": [{"system": "urn:iso:std:iso:11073:10101", "code": "131329"}]},
            "subject":   {"reference": "Patient/test"},
            "encounter": {"reference": "Encounter/test"},
            "effectiveDateTime": "2099-12-29T22:01:55Z",
            "valueSampledData": {
                "origin": {"value": 0.0, "unit": "mV"},
                "period": 1000.0 / srate,
                "factor": 0.00015259,
                "dimensions": 1,
                "data": data,
            },
        }},
    ],
}
req = urllib.request.Request(
    BASE + "/fhir/\$ecg-qrs-detector",
    data=json.dumps(params).encode("utf-8"),
    method="POST",
    headers={"Content-Type": "application/fhir+json"},
)
with urllib.request.urlopen(req, timeout=60) as r:
    bundle = json.loads(r.read())
    if r.status != 200 or bundle.get("resourceType") != "Bundle":
        fail(f"POST status={r.status} rt={bundle.get('resourceType')}")
print(f"  POST \$ecg-qrs-detector: Bundle entries={len(bundle.get('entry', []))}")
print("  ✓ FHIR live test OK")
PYEOF
fi

echo
echo "=== done ==="
echo "  image:  $IMAGE_REMOTE"
echo "  digest: $DIGEST"
