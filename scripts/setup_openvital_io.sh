#!/usr/bin/env bash
#
# Idempotent setup for api.openvital.io as a second custom domain on the
# existing API Gateway v2 (HTTP API) that currently serves
# filter.vitaldb.net. Every step is gated by a "does it already exist?"
# check so reruns are safe.
#
# What this does:
#   1. Ensure Route 53 hosted zone for openvital.io exists.
#   2. Request (or reuse) an ACM cert for openvital.io + *.openvital.io
#      in the same region as the existing API Gateway (REGIONAL endpoint
#      type — cert must live in the API's region, NOT us-east-1).
#   3. Write the cert's DNS validation CNAMEs into the hosted zone.
#   4. Wait until the cert is ISSUED (skipped unless NS_DELEGATED=1).
#   5. Create the APIGW v2 custom domain api.openvital.io with that cert.
#   6. Map the new custom domain (root path) to the same HTTP API + stage
#      that filter.vitaldb.net is mapped to, so both domains share one
#      backend.
#   7. Create an A-alias api.openvital.io → APIGW regional domain name in
#      Route 53.
#
# What this does NOT do:
#   - Register openvital.io. Do that once via Route 53 → Registered
#     domains, or via any registrar with NS records pointed at the zone
#     this script creates.
#   - Deploy or update the Lambda container. Run
#     ./scripts/deploy_lambda.sh for that.
#
# Required:
#   - aws CLI with credentials that can do route53:*, acm:*, apigateway:*.
#   - python3 (or python — auto-detected).
#   - An existing APIGW v2 custom domain (default: filter.vitaldb.net) so
#     the script can mirror its API mapping. Override with
#     EXISTING_DOMAIN env var.
#
# Usage:
#   ./scripts/setup_openvital_io.sh                              # steps 1–3 then exit
#   NS_DELEGATED=1 ./scripts/setup_openvital_io.sh               # full run
#   PROFILE=other NS_DELEGATED=1 ./scripts/setup_openvital_io.sh # named profile
#
set -euo pipefail

PROFILE=${PROFILE:-}
DOMAIN=openvital.io
SUBDOMAIN=api.$DOMAIN
EXISTING_DOMAIN=${EXISTING_DOMAIN:-filter.vitaldb.net}
REGION=${REGION:-ap-northeast-2}

if [ -n "$PROFILE" ]; then
  PROFILE_ARGS=(--profile "$PROFILE")
else
  PROFILE_ARGS=()
fi
aws_() { aws "${PROFILE_ARGS[@]}" "$@"; }

SCRATCH=${TMPDIR:-${TEMP:-/tmp}}

# Pick whichever Python interpreter is real. On Windows, `python3` often
# resolves to a Microsoft Store stub that prints an install hint and exits
# nonzero, so prefer `python` when it works.
PY=
if command -v python3 >/dev/null 2>&1 && python3 -c "import sys" >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1 && python -c "import sys" >/dev/null 2>&1; then
  PY=python
else
  echo "✗ neither python3 nor python is available on PATH" >&2; exit 1
fi

echo "=== api.openvital.io setup (APIGW v2 REGIONAL) ==="
echo "  profile:           ${PROFILE:-<default-chain>}"
echo "  region:            $REGION"
echo "  domain:            $DOMAIN"
echo "  subdomain:         $SUBDOMAIN"
echo "  mirror existing:   $EXISTING_DOMAIN"
echo

# ---------------------------------------------------------------------------
# Step 1: Route 53 hosted zone
# ---------------------------------------------------------------------------
echo "--- step 1: Route 53 hosted zone ---"
ZONE_ID=$(aws_ route53 list-hosted-zones-by-name --dns-name "$DOMAIN." \
  --query "HostedZones[?Name=='$DOMAIN.'].Id | [0]" --output text 2>/dev/null || true)
ZONE_ID=${ZONE_ID#/hostedzone/}

if [ -z "$ZONE_ID" ] || [ "$ZONE_ID" = "None" ]; then
  echo "  creating hosted zone for $DOMAIN..."
  ZONE_ID=$(aws_ route53 create-hosted-zone \
    --name "$DOMAIN" \
    --caller-reference "openvital-io-$(date +%s)" \
    --hosted-zone-config Comment="openvital.io public services" \
    --query 'HostedZone.Id' --output text)
  ZONE_ID=${ZONE_ID#/hostedzone/}
  echo "  ✓ created: $ZONE_ID"
  echo
  echo "  ↳ POINT YOUR REGISTRAR'S NS RECORDS AT:"
  aws_ route53 get-hosted-zone --id "$ZONE_ID" \
    --query 'DelegationSet.NameServers' --output table
else
  echo "  ✓ already exists: $ZONE_ID"
fi
echo

# ---------------------------------------------------------------------------
# Step 2: ACM cert in REGION (APIGW REGIONAL endpoints need same-region certs)
# ---------------------------------------------------------------------------
echo "--- step 2: ACM cert in $REGION ---"
CERT_ARN=$(aws_ acm list-certificates --region "$REGION" \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" \
  --output text 2>/dev/null || true)

if [ -z "$CERT_ARN" ] || [ "$CERT_ARN" = "None" ]; then
  echo "  requesting cert for $DOMAIN + *.$DOMAIN..."
  CERT_ARN=$(aws_ acm request-certificate --region "$REGION" \
    --domain-name "$DOMAIN" \
    --subject-alternative-names "*.$DOMAIN" \
    --validation-method DNS \
    --query 'CertificateArn' --output text)
  echo "  ✓ requested: $CERT_ARN"
else
  echo "  ✓ already exists: $CERT_ARN"
fi
echo

# ---------------------------------------------------------------------------
# Step 3: DNS validation CNAMEs
# ---------------------------------------------------------------------------
echo "--- step 3: DNS validation CNAMEs ---"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  VAL_JSON=$(aws_ acm describe-certificate --region "$REGION" \
    --certificate-arn "$CERT_ARN" \
    --query 'Certificate.DomainValidationOptions' --output json)
  if "$PY" -c "
import sys, json
try:
    v = json.loads(sys.argv[1])
    sys.exit(0 if v and v[0].get('ResourceRecord', {}).get('Name') else 1)
except Exception:
    sys.exit(1)
" "$VAL_JSON" >/dev/null 2>&1; then
    break
  fi
  echo "  waiting for ACM to populate validation records (attempt $attempt)..."
  sleep 5
done

CHANGE_FILE="$SCRATCH/openvital-acm-validation.json"
"$PY" - "$VAL_JSON" "$CHANGE_FILE" <<'PYEOF'
import sys, json
val = json.loads(sys.argv[1])
seen = set()
changes = []
for v in val:
    rr = v.get("ResourceRecord") or {}
    name = rr.get("Name")
    if not name or name in seen:
        continue
    seen.add(name)
    changes.append({
        "Action": "UPSERT",
        "ResourceRecordSet": {
            "Name": name,
            "Type": rr.get("Type", "CNAME"),
            "TTL": 300,
            "ResourceRecords": [{"Value": rr["Value"]}],
        },
    })
with open(sys.argv[2], "w") as f:
    json.dump({"Comment": "ACM validation", "Changes": changes}, f)
print(f"  staged {len(changes)} validation record(s) → {sys.argv[2]}")
PYEOF

aws_ route53 change-resource-record-sets \
  --hosted-zone-id "$ZONE_ID" \
  --change-batch "file://$CHANGE_FILE" \
  --query 'ChangeInfo.Status' --output text
echo "  ✓ validation records upserted"
echo

# ---------------------------------------------------------------------------
# Step 4: wait for cert ISSUED
# ---------------------------------------------------------------------------
echo "--- step 4: wait for ACM cert to be ISSUED ---"
if [ "${NS_DELEGATED:-0}" != "1" ]; then
  STATUS=$(aws_ acm describe-certificate --region "$REGION" \
    --certificate-arn "$CERT_ARN" \
    --query 'Certificate.Status' --output text)
  echo "  current: $STATUS  (NS_DELEGATED=0, skipping the 10-min wait)"
  if [ "$STATUS" != "ISSUED" ]; then
    echo
    echo "=== partial done (steps 1–3 complete) ==="
    echo "  Point your registrar's NS records at this zone, then rerun with"
    echo "  NS_DELEGATED=1 ./scripts/setup_openvital_io.sh"
    echo "  to wait for ISSUED and continue with APIGW + A-alias."
    exit 0
  fi
else
  for attempt in $(seq 1 60); do
    STATUS=$(aws_ acm describe-certificate --region "$REGION" \
      --certificate-arn "$CERT_ARN" \
      --query 'Certificate.Status' --output text)
    echo "  attempt $attempt: $STATUS"
    if [ "$STATUS" = "ISSUED" ]; then break; fi
    if [ "$STATUS" = "FAILED" ]; then
      echo "  ✗ cert request failed" >&2; exit 1
    fi
    sleep 10
  done
  if [ "$STATUS" != "ISSUED" ]; then
    echo "  cert still not ISSUED after 10 min — verify NS delegation" >&2
    echo "    dig +short NS $DOMAIN @8.8.8.8" >&2
    exit 1
  fi
fi
echo

# ---------------------------------------------------------------------------
# Step 5: mirror the API mapping from EXISTING_DOMAIN
# ---------------------------------------------------------------------------
echo "--- step 5: discover API binding on $EXISTING_DOMAIN ---"
MIRROR_API_ID=$(aws_ apigatewayv2 get-api-mappings \
  --region "$REGION" --domain-name "$EXISTING_DOMAIN" \
  --query 'Items[0].ApiId' --output text 2>/dev/null || true)
MIRROR_STAGE=$(aws_ apigatewayv2 get-api-mappings \
  --region "$REGION" --domain-name "$EXISTING_DOMAIN" \
  --query 'Items[0].Stage' --output text 2>/dev/null || true)
if [ -z "$MIRROR_API_ID" ] || [ "$MIRROR_API_ID" = "None" ]; then
  echo "  ✗ no APIGW v2 mapping found on $EXISTING_DOMAIN — set EXISTING_DOMAIN env var." >&2
  exit 1
fi
echo "  ✓ mirror target: API=$MIRROR_API_ID stage=$MIRROR_STAGE"
echo

# ---------------------------------------------------------------------------
# Step 6: APIGW v2 custom domain for $SUBDOMAIN
# ---------------------------------------------------------------------------
echo "--- step 6: APIGW v2 custom domain $SUBDOMAIN ---"
DN_INFO=$(aws_ apigatewayv2 get-domain-name --region "$REGION" \
  --domain-name "$SUBDOMAIN" 2>/dev/null || true)
if [ -z "$DN_INFO" ]; then
  echo "  creating custom domain..."
  aws_ apigatewayv2 create-domain-name --region "$REGION" \
    --domain-name "$SUBDOMAIN" \
    --domain-name-configurations "CertificateArn=$CERT_ARN,EndpointType=REGIONAL,SecurityPolicy=TLS_1_2" \
    --query 'DomainNameConfigurations[0].[ApiGatewayDomainName,HostedZoneId]' --output text
  DN_INFO=$(aws_ apigatewayv2 get-domain-name --region "$REGION" --domain-name "$SUBDOMAIN")
  echo "  ✓ created"
else
  echo "  ✓ already exists"
fi
APIGW_DOMAIN=$("$PY" -c "
import sys, json
d = json.loads(sys.argv[1])
print(d['DomainNameConfigurations'][0]['ApiGatewayDomainName'])
" "$DN_INFO")
APIGW_HZID=$("$PY" -c "
import sys, json
d = json.loads(sys.argv[1])
print(d['DomainNameConfigurations'][0]['HostedZoneId'])
" "$DN_INFO")
echo "  target: $APIGW_DOMAIN  (hosted-zone $APIGW_HZID)"
echo

# ---------------------------------------------------------------------------
# Step 7: API mapping (root path → mirror API + stage)
# ---------------------------------------------------------------------------
echo "--- step 7: API mapping $SUBDOMAIN (root) → $MIRROR_API_ID/$MIRROR_STAGE ---"
EXISTING_MAP=$(aws_ apigatewayv2 get-api-mappings \
  --region "$REGION" --domain-name "$SUBDOMAIN" \
  --query "Items[?ApiId=='$MIRROR_API_ID' && Stage=='$MIRROR_STAGE'].ApiMappingId | [0]" \
  --output text 2>/dev/null || true)
if [ -z "$EXISTING_MAP" ] || [ "$EXISTING_MAP" = "None" ]; then
  aws_ apigatewayv2 create-api-mapping --region "$REGION" \
    --domain-name "$SUBDOMAIN" \
    --api-id "$MIRROR_API_ID" \
    --stage "$MIRROR_STAGE" \
    --query 'ApiMappingId' --output text
  echo "  ✓ created"
else
  echo "  ✓ already mapped: $EXISTING_MAP"
fi
echo

# ---------------------------------------------------------------------------
# Step 8: Route 53 A-alias record
# ---------------------------------------------------------------------------
echo "--- step 8: A-alias $SUBDOMAIN → $APIGW_DOMAIN ---"
EXISTING_TYPE=$(aws_ route53 list-resource-record-sets \
  --hosted-zone-id "$ZONE_ID" \
  --query "ResourceRecordSets[?Name=='$SUBDOMAIN.'].Type | [0]" \
  --output text 2>/dev/null || true)
if [ "$EXISTING_TYPE" = "A" ]; then
  echo "  ✓ A-alias already exists"
else
  ALIAS_FILE="$SCRATCH/openvital-apigw-alias.json"
  cat > "$ALIAS_FILE" <<EOF
{
  "Comment": "$SUBDOMAIN → APIGW v2 ($MIRROR_API_ID) regional $REGION",
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "$SUBDOMAIN",
      "Type": "A",
      "AliasTarget": {
        "HostedZoneId": "$APIGW_HZID",
        "DNSName": "$APIGW_DOMAIN",
        "EvaluateTargetHealth": false
      }
    }
  }]
}
EOF
  aws_ route53 change-resource-record-sets \
    --hosted-zone-id "$ZONE_ID" \
    --change-batch "file://$ALIAS_FILE" \
    --query 'ChangeInfo.Status' --output text
  echo "  ✓ A-alias created"
fi
echo

echo "=== done ==="
echo "  https://$SUBDOMAIN/                              (legacy filter list)"
echo "  https://$SUBDOMAIN/fhir/metadata                 (after Lambda redeploy)"
echo "  https://$SUBDOMAIN/fhir/OperationDefinition/ecg-qrs-detector"
echo
echo "  Smoke-test:"
echo "    $PY -c \"import urllib.request, json; print(len(json.loads(urllib.request.urlopen('https://$SUBDOMAIN/').read())))\""
