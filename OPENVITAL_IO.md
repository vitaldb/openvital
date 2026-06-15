# openvital.io — serverless hosting on AWS

How the public `api.openvital.io` domain is fronted by the same Lambda
container that already serves `filter.vitaldb.net`. Refer to
[`LAMBDA.md`](LAMBDA.md) for the container itself; this file covers the
domain wiring.

## Architecture

```
       openvital.io                            (registered externally;
              │                                 NS pointed at the Route 53
              │  Route 53 hosted zone           hosted zone)
              ▼
   api.openvital.io  ─ A-alias ─▶  APIGW v2 HTTP API
                                   custom domain (REGIONAL, ap-northeast-2)
                                   target: d-xxxxxxx.execute-api…
                                           │
                                           │ API mapping (root) → $default stage
                                           ▼
                                  HTTP API "openvital-filter"
                                  (id rajpvo0wd1)
                                           │
                                           ▼
                                  Lambda function "openvital-filter"
                                  container image (ECR)
                                  ap-northeast-2
```

Both `filter.vitaldb.net` (VR) and `api.openvital.io` (public) are
**custom domains on the same HTTP API**, mapped at root to the same
`$default` stage. One Lambda, two domains, distinct ACM certs (each
domain owns its own cert in the API's region).

Why no CloudFront: API Gateway REGIONAL endpoints serve TLS directly. A
front CloudFront would add a hop and only pays off for global edge
caching, which the filter workload doesn't benefit from (each call has
unique payload).

## Endpoints

| Domain                | Route                                    | Purpose                            |
| --------------------- | ---------------------------------------- | ---------------------------------- |
| `filter.vitaldb.net`  | `GET /`                                  | legacy filter list (VR)            |
| `filter.vitaldb.net`  | `POST /<mod>`                            | legacy gzip-JSON filter (VR)       |
| `api.openvital.io`    | `GET /fhir/metadata`                     | CapabilityStatement                |
| `api.openvital.io`    | `GET /fhir/OperationDefinition/<filter>` | spec per filter                    |
| `api.openvital.io`    | `POST /fhir/$<filter>`                   | FHIR Operation invocation          |

Both domains accept both wire formats — the URL prefix dispatches in
`openvital/__main__.py`. The split above is just policy ("VR uses
legacy, public uses FHIR"); nothing enforces it server-side.

## One-time setup

### 1. Register `openvital.io`

Either:
- Route 53 → Registered domains → Register domain (~$14/yr for `.io`), or
- Use any external registrar and set the NS records to the Route 53
  hosted zone created in step 2 (see step 2 output for the 4 NS values).

### 2. Run the setup script

```bash
./scripts/setup_openvital_io.sh
```

The script is idempotent. First run handles steps 1–3 and exits while
you delegate NS at the registrar:

1. Route 53 hosted zone for `openvital.io` (if missing) — prints the 4 NS
   records to set at your registrar.
2. ACM certificate request for `openvital.io` + `*.openvital.io` in
   `ap-northeast-2` (REGIONAL APIGW endpoints need a same-region cert,
   NOT us-east-1).
3. DNS validation CNAMEs written into the hosted zone.

Once NS delegation has propagated (`dig +short NS openvital.io @8.8.8.8`
returns the AWS NS), rerun with `NS_DELEGATED=1` to finish:

```bash
NS_DELEGATED=1 ./scripts/setup_openvital_io.sh
```

This completes steps 4–8:

4. Polls until the cert reaches `ISSUED` (usually 1–2 min once NS is
   live).
5. Discovers the HTTP API + stage that `filter.vitaldb.net` is mapped
   to, so the new domain mirrors that backend.
6. Creates the APIGW v2 custom domain `api.openvital.io` with the new
   cert (REGIONAL, TLS_1_2).
7. Creates the API mapping `api.openvital.io` (root) → same API +
   stage.
8. Creates the Route 53 A-alias `api.openvital.io` → APIGW regional
   domain name (correct regional hosted zone ID is read from the APIGW
   response — no hardcoded ID).

### 3. Redeploy the Lambda (one-time, picks up the FHIR routes)

```bash
./scripts/deploy_lambda.sh
```

The same image powers both domains. The deploy script's live test now
exercises `/fhir/metadata`, `/fhir/OperationDefinition/ecg-qrs-detector`,
and `POST /fhir/$ecg-qrs-detector` in addition to `GET /`.

### 4. Smoke-test

```bash
# Legacy route (works as soon as DNS + APIGW propagate, even before
# the FHIR Lambda redeploy):
python -c "import urllib.request, json; \
  d = json.loads(urllib.request.urlopen('https://api.openvital.io/').read()); \
  print(len(d), 'filters')"

# FHIR routes (need the FHIR-aware Lambda image deployed):
curl -s https://api.openvital.io/fhir/metadata \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d['fhirVersion'], len(d['rest'][0]['operation']))"
# 4.0.1 16
```

> Note: `curl` on Windows uses schannel, which sometimes rejects the
> Amazon-issued cert chain even when the cert is valid. If a Windows
> `curl` fails with `SEC_E_WRONG_PRINCIPAL`, retest with Python's
> `urllib.request.urlopen` (uses certifi) or a browser — those are the
> authoritative checks.

## Cost expectation

For low-traffic public hosting (≤1 M req/mo, ≤50 GB egress):

| Item                       | Estimated $/mo |
| -------------------------- | --------------:|
| Route 53 hosted zone       |          $0.50 |
| ACM certificate            |          $0.00 |
| APIGW v2 custom domain     |       included |
| Lambda (shared with VR)    |       included |
| Domain renewal (`.io`)     |       ~$1.00 (amortised) |
| **Net additional**         |     **~$1.50** |

The Lambda itself stays on the existing usage tier; this change only
fronts it with a second domain.

## Required IAM permissions

The setup script needs these actions on the calling principal:

```
route53:ListHostedZonesByName, CreateHostedZone, GetHostedZone,
        ChangeResourceRecordSets, ListResourceRecordSets
acm:ListCertificates, RequestCertificate, DescribeCertificate
apigateway:GET (covers the v1/v2 read paths)
apigatewayv2:GetDomainName, CreateDomainName,
             GetApiMappings, CreateApiMapping
```

The deploy script additionally needs ECR + Lambda update perms (see
[`LAMBDA.md`](LAMBDA.md)).

## When to split into a second API / Lambda

Keep the shared setup unless any of these become true:

- API quotas (rate limit, throttle) start conflicting between the two
  audiences.
- Public openvital.io traffic grows large enough that VR-side latency
  budgets get squeezed by neighbour cold starts.
- The `openvital[ml-tf,ml-torch]` extras need to ship on Lambda, in which
  case the heavy filters go to a second container image (see
  [`LAMBDA.md`](LAMBDA.md) §"Cold-start behavior").

## Manual revert

If you ever need to remove `api.openvital.io` without touching VR:

1. Delete the API mapping on the custom domain.
2. Delete the APIGW v2 custom domain `api.openvital.io`.
3. Delete the Route 53 A-alias record.
4. Optional: delete the ACM cert in ap-northeast-2.
5. Leave the hosted zone in place if you might want it back ($0.50/mo).
