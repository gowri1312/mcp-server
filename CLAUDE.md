# Starter Prompt — Build & Host an MCP Server on Azure

> **What this is.** A reusable kickoff prompt for standing up *any* Model Context Protocol (MCP) server on
> an Azure subscription — a stateless API wrapper, a document-search server, a data-pipeline server, an
> internal-tooling server, anything. Paste the whole document into Claude Code (or hand it to a teammate)
> as the brief. It encodes the architecture, the exact Azure resources, the deploy pipeline, and **two**
> authentication tiers: a shared API key, and OAuth 2.1 via Microsoft Entra ID.
>
> It is opinionated on purpose — these are defaults that hold up in production. Where a choice depends on
> your server's shape (stateless vs. stateful, internal vs. user-facing), the prompt forks explicitly
> instead of guessing. Replace every `<angle-bracket>` placeholder.

---

## 0. Your role & the definition of done

You are a platform engineer standing up a **remote, HTTP-transport MCP server** on Azure. The finished system:

1. Runs as a container on **Azure Container Apps (ACA)**, pulled from **Azure Container Registry (ACR)** via a **user-assigned managed identity** — no registry passwords anywhere.
2. Speaks **MCP over Streamable HTTP** at `https://<fqdn>/mcp`. **Never SSE** — that transport is deprecated in the MCP spec; do not enable it.
3. Exposes an **unauthenticated `/health`** route (for ACA probes) and authenticates every other route.
4. Keeps all secrets in **Key Vault**, surfaced to the container as ACA secret refs — never baked into the image, never plaintext env, never logged.
5. Ships from a **versioned Bicep + CI pipeline** wired to **GitHub** with **OIDC federation** — no stored cloud credentials in GitHub.
6. Supports **two auth modes** behind a flag: an API key (fast; good for service-to-service) and **OAuth 2.1 via Entra ID** (preferred for user-facing clients such as Claude Desktop / Claude Code).

**Done means:** a teammate can run `curl https://<fqdn>/health` → `OK`, complete an MCP `initialize`
handshake with a valid credential, get `401` without one, and redeploy from a `git push`.

**Before you build, answer two questions — they drive every later fork:**

- **Stateful or stateless?** Does the server keep local state on disk (an embedded DB, a search index, a
  cache)? Stateless is the happy path: scale out, no file share. Stateful needs persistence and a single writer.
- **Internal or user-facing?** Machine-to-machine you control → an API key is fine. Interactive clients
  where a human should sign in → OAuth via Entra.

---

## 1. Prerequisites — install & verify first

```bash
az version                     # Azure CLI ≥ 2.60
az bicep version               # Bicep ≥ 0.26   (az bicep install)
docker --version               # only if building locally; ACR can build for you
gh --version                   # GitHub CLI, for repo + secrets wiring
python --version               # 3.12+  (reference runtime: python:3.12-slim)
```

Azure-side:

- A **subscription** you can create resource groups in, with **Owner** or **Contributor + User Access Administrator** (you'll create role assignments).
- For OAuth: permission to **register applications in Entra ID** (Application Developer at minimum; exposing an API scope + admin consent needs an admin).
- A **region** with the services you need. `eastus2` has broad availability; pick for data residency if it matters.

```bash
az login
az account set --subscription "<subscription-id-or-name>"
az account show -o table
```

---

## 2. Reference architecture

```
┌──────────────────────────────┐         Streamable HTTP (HTTPS)
│  MCP client                  │  ───────────────────────────────────►  https://<app>.<region>.azurecontainerapps.io/mcp
│  Claude Desktop / Code,      │                                         │
│  agent runtime, curl         │  ◄── 401 + WWW-Authenticate (OAuth) ────┘
└──────────────────────────────┘
                                              ▼
                            ┌─────────────────────────────────────────────┐
                            │  Azure Container Apps (managed env)          │
                            │  ┌───────────────────────────────────────┐  │
                            │  │ Container: MCP server (port 8000)     │  │
                            │  │  • /health  (open)                    │  │
                            │  │  • /mcp     (authenticated)           │  │
                            │  │  • auth: API key  OR  Entra OAuth     │  │
                            │  └───────────────────────────────────────┘  │
                            │   identity: user-assigned MI (AcrPull)      │
                            │   secrets:  ACA secretRefs ← Key Vault      │
                            │   volume:   Azure File share → /app/data    │  ← stateful servers only
                            │   ingress:  external HTTPS, transport=http  │
                            │   scale:    stateless → out; stateful → 1   │
                            └─────────────────────────────────────────────┘
                              ▲                 ▲                  ▲
                              │ AcrPull         │ secret reads     │ files (stateful only)
                    ┌─────────┴──────┐  ┌────────┴───────┐  ┌──────┴────────────┐
                    │ ACR (registry) │  │ Key Vault      │  │ Storage acct +    │
                    │ your image     │  │ api-key, creds │  │ File share (data) │
                    └────────────────┘  └────────────────┘  └───────────────────┘

   Entra ID (OAuth path):  App Registration ("Expose an API" → scopes) is the upstream Authorization Server.
```

**Why these choices:**

- **ACA over AKS / App Service** — serverless containers with built-in ingress, TLS, and revisions; scale-to-zero capable; far less to operate than Kubernetes. The right altitude for a single service.
- **User-assigned managed identity** — the container pulls from ACR (and optionally reads Key Vault) with **no passwords**. The single biggest security win over connection strings.
- **Azure File share at `/app/data`** — *only* if the server keeps local state. A purely stateless server **skips the share** and scales out freely.

---

## 3. The MCP server skeleton

The examples use **Python + FastMCP 3.x** (mature, batteries-included auth). The same architecture applies
to a TypeScript (`@modelcontextprotocol/sdk`) or any other MCP server — keep the contract: Streamable HTTP,
open `/health`, authenticated `/mcp`, port 8000.

```python
# server.py
import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("mcp")


@asynccontextmanager
async def lifespan(app):
    logger.info("starting MCP server")
    # init clients / DB / warm caches here (stateful servers)
    yield
    logger.info("shutting down")


mcp = FastMCP("my-mcp", lifespan=lifespan, auth=build_auth(settings))   # auth: see §6


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    # MUST stay unauthenticated — ACA startup/liveness probes hit this.
    return PlainTextResponse("OK")


@mcp.tool()
async def example_tool(query: str, limit: int = 10) -> str:
    """One clear sentence the model reads to decide *when* to call this.

    Args:
        query: what the caller is looking for.
        limit: max results, 1-50.
    """
    try:
        return do_work(query, limit)
    except Exception as e:                       # never leak a stack trace to the client
        logger.exception("example_tool failed")
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=settings.port)   # Streamable HTTP, not "sse"
```

**Tool-authoring rules — these materially change how well an agent uses your server:**

- The **docstring is the tool's UI** for the model. Lead with *when* to use it; document every arg with units/ranges.
- **Catch exceptions inside each tool** and return a readable `Error: ...` string. An unhandled exception becomes an opaque protocol error.
- Return **compact, structured** output (markdown tables / small JSON), not giant blobs — token budget is real.
- **Name tools by intent**, not by endpoint (`find_customer`, not `get_api_v2_customers`).
- **Long-running work (>~10 s):** offload to a background thread/task and return immediately with a "poll `status()`" hint, so you don't trip client timeouts or block the serving loop.
- Keep a lightweight **`status()`** tool so operators can introspect the running server through MCP itself.
- Fewer, well-described tools beat many overlapping ones — an overloaded tool list degrades model routing.

### Config from env (Pydantic)

```python
# config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    port: int = 8000
    auth_mode: str = "apikey"            # "apikey" | "oauth" | "none"

    mcp_api_key: str = ""                # apikey mode

    entra_tenant_id: str = ""            # oauth mode
    entra_client_id: str = ""
    entra_client_secret: str = ""
    public_base_url: str = ""            # https://<fqdn> — what clients see

settings = Settings()
```

### Dockerfile (non-root, slim)

```dockerfile
FROM python:3.12-slim
RUN groupadd -r app && useradd -r -g app -m -d /home/app app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chown -R app:app /app
USER app                                 # never run the container as root
ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
CMD ["python", "server.py"]
```

---

## 4. Provision Azure (idempotent, scripted)

Use **az CLI for the one-time scaffold** (resource group, ACR, ACA env, Key Vault) and **Bicep for the app**
so the container, identity, role assignment, secrets, ingress, and probes are versioned and re-runnable.

```bash
# ── names (lowercase; globally-unique where noted) ──────────────────────────
RG=rg-gowri-mcp-001
LOC=Australiasoutheast
ACR=<acrnameunique>                # 5-50 alphanumeric, globally unique
ACA_ENV=cae-<name>
KV=<kv-name-unique>                # globally unique
APP=<app-name>

az group create -n $RG -l $LOC
az acr create -g $RG -n $ACR --sku Basic -l $LOC
az containerapp env create -g $RG -n $ACA_ENV -l $LOC --logs-destination none
az keyvault create -g $RG -n $KV -l $LOC --enable-rbac-authorization true

# secrets
az keyvault secret set --vault-name $KV --name mcp-api-key --value "$(openssl rand -hex 32)"
# OAuth path also stores: entra-client-secret  (from §6)

# build the image *in ACR* (no local Docker needed)
az acr build -r $ACR -t $APP:latest .
```

### Bicep for the app (key fragments)

```bicep
// user-assigned identity + AcrPull (no registry passwords)
resource uai 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-id'
  location: location
}
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uai.id, 'acrpull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull
    principalId: uai.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${uai.id}': {} } }
  properties: {
    managedEnvironmentId: acaEnvId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'          // Streamable HTTP. allowInsecure:false → HTTPS enforced.
        allowInsecure: false
      }
      secrets: [
        { name: 'mcp-api-key',         value: mcpApiKey }          // @secure() params
        { name: 'entra-client-secret', value: entraClientSecret }
      ]
      registries: [ { server: acr.properties.loginServer, identity: uai.id } ]
    }
    template: {
      containers: [ {
        name: appName
        image: containerImage
        env: [
          { name: 'PORT',                value: '8000' }
          { name: 'AUTH_MODE',           value: authMode }          // 'apikey' | 'oauth'
          { name: 'PUBLIC_BASE_URL',     value: 'https://${appName}.${envFqdnSuffix}' }
          { name: 'MCP_API_KEY',         secretRef: 'mcp-api-key' }
          { name: 'ENTRA_TENANT_ID',     value: entraTenantId }
          { name: 'ENTRA_CLIENT_ID',     value: entraClientId }
          { name: 'ENTRA_CLIENT_SECRET', secretRef: 'entra-client-secret' }
        ]
        probes: [
          { type: 'Startup',  httpGet: { port: 8000, path: '/health' }, periodSeconds: 15, failureThreshold: 40 }
          { type: 'Liveness', httpGet: { port: 8000, path: '/health' }, periodSeconds: 30, failureThreshold: 10 }
        ]
        resources: { cpu: json('0.5'), memory: '1Gi' }   // size to your workload
      } ]
      scale: { minReplicas: 0, maxReplicas: 5 }          // STATELESS default. Stateful → min:1, max:1.
    }
  }
}
output mcpUrl string = 'https://${app.properties.configuration.ingress.fqdn}/mcp'
```

Deploy:

```bash
ENV_ID=$(az containerapp env show -g $RG -n $ACA_ENV --query id -o tsv)
MCP_KEY=$(az keyvault secret show --vault-name $KV --name mcp-api-key --query value -o tsv)

az deployment group create -g $RG \
  --template-file deploy/main.bicep \
  --parameters appName=$APP \
    containerImage="$ACR.azurecr.io/$APP:latest" \
    acaEnvId="$ENV_ID" authMode="apikey" mcpApiKey="$MCP_KEY"
```

> **Probe sizing matters.** If the image downloads models or warms a cache on first boot, the **startup
> probe** must allow enough total grace (`periodSeconds × failureThreshold`) or ACA kills the container
> before it's ready. **Liveness** must be generous enough not to reap the container during legitimate long work.

---

## 5. Persistence & scaling — pick deliberately

- **Stateless server** (most MCP servers — API wrappers, search-over-remote-store, compute):
  - No file share. `minReplicas: 0` (cheapest; accept a cold start) up to `maxReplicas: N`.
  - Externalize any state to a managed service (**Azure Postgres Flexible Server**, **Cosmos DB**, **Azure AI Search**, **Blob**) reached via the managed identity. This keeps the server horizontally scalable.

- **Stateful server** (embedded DB / on-disk index / local cache):
  - Mount an **Azure File share** at the data dir so state survives restarts and revisions.
  - **Pin to one replica** (`minReplicas: 1, maxReplicas: 1`) unless your store does true multi-writer
    locking. An embedded DB (e.g. SQLite) over a network share + concurrent writers → corruption and
    `database is locked`. An in-process scheduler also double-fires across replicas.
  - Run heavy CPU work on a **dedicated thread** (not the asyncio serving loop) so `/health` keeps
    answering and liveness doesn't reap you. If a background job OOMs the request path, split it into a
    separate **ACA Job** rather than sharing memory with the server.

> **Don't cargo-cult the single replica.** It's a *consequence* of local state, not a default. When in
> doubt, design stateless and push state to a managed store — it's the more scalable, more operable choice.

---

## 6. Authentication

Two tiers. FastMCP can run **both at once** (`MultiAuth`) during a migration.

### Tier A — API key (fast; service-to-service)

Accept the key via several channels so clients that can't set custom headers still work, and **always leave
`/health` open**:

```python
import hmac
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health" or not settings.mcp_api_key:
            return await call_next(request)
        key = (
            request.headers.get("x-api-key")
            or request.headers.get("authorization", "").replace("Bearer ", "").strip()
            or request.query_params.get("api_key")
            or request.query_params.get("code")      # Azure "?code=" alias
            or ""
        )
        if not hmac.compare_digest(key, settings.mcp_api_key):   # constant-time
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

mcp.run(transport="http", host="0.0.0.0", port=settings.port,
        middleware=[Middleware(ApiKeyMiddleware)])
```

**Trade-offs, and how to mitigate them:**

| Concern | Mitigation |
|---|---|
| **Query-string keys leak** into server/proxy logs, history, referer headers | Prefer the `x-api-key` **header**. Treat `?api_key=` / `?code=` as a compatibility fallback only, and **scrub query strings from access logs**. |
| Single shared secret = no per-client identity or granular revocation | Issue **distinct keys per consumer** (compare against a set) so you can revoke one without rotating all. |
| Static, long-lived | **Rotate** on a schedule; keep two valid keys during the overlap. |
| Timing leak on compare | Use `hmac.compare_digest`, never `==`. |

API key is right for **machine-to-machine** you control. It is **not** right for interactive clients where
a human should authenticate — that's OAuth.

### Tier B — OAuth 2.1 via Microsoft Entra ID (preferred for user-facing)

**How MCP auth works** (spec revision 2025-06-18): your MCP server is an **OAuth 2.0 Resource Server**. It:

1. Returns `401` with `WWW-Authenticate: Bearer resource_metadata="https://<fqdn>/.well-known/oauth-protected-resource"`.
2. Serves **Protected Resource Metadata** (RFC 9728) pointing at the **Authorization Server** (Entra ID).
3. The client discovers the AS, runs **OAuth 2.1 with PKCE**, and gets an access token.
4. The client calls `/mcp` with `Authorization: Bearer <token>`; the server **validates the JWT** (JWKS signature, `iss`, `aud`, expiry, required scopes).

Entra doesn't support the open **Dynamic Client Registration** that MCP clients expect, so FastMCP's
`AzureProvider` runs an **OAuth Proxy**: it presents the discovery + DCR-like endpoints to the MCP client
while brokering to **one pre-registered Entra app** upstream. You get spec-compliant MCP auth without every
client needing its own Entra registration.

**Step 1 — register the app & expose a scope** (admin needed for consent):

```bash
APP_ID=$(az ad app create --display-name "$APP" --query appId -o tsv)

# client secret (or — preferred for prod — a federated credential / certificate)
az ad app credential reset --id $APP_ID --append --display-name "mcp-secret" --years 1 --query password -o tsv
# → store in Key Vault as entra-client-secret

# Expose an API: set the Application ID URI, then add a scope (e.g. mcp.invoke)
az ad app update --id $APP_ID --identifier-uris "api://$APP_ID"
# Portal → App registration → Expose an API → Add a scope: mcp.invoke (Admins+users)

TENANT_ID=$(az account show --query tenantId -o tsv)
# Register the proxy redirect URI on the app's "Web" platform:
#   https://<fqdn>/auth/callback   (and http://localhost:8000/auth/callback for dev)
```

**Step 2 — wire the provider** (full interactive OAuth proxy — the recommended default):

```python
from fastmcp.server.auth.providers.azure import AzureProvider

def build_auth(settings):
    if settings.auth_mode == "oauth":
        return AzureProvider(
            client_id=settings.entra_client_id,
            client_secret=settings.entra_client_secret,
            tenant_id=settings.entra_tenant_id,        # your tenant GUID, not "common"
            required_scopes=["mcp.invoke"],            # the scope you exposed
            base_url=settings.public_base_url,         # https://<fqdn> — MUST equal the ingress FQDN
            # identifier_uri="api://<app-id>",         # if the token audience differs from client_id
            # redirect_path="/auth/callback",          # must be registered on the app
            require_authorization_consent=True,
        )
    return None    # apikey mode → enforced via middleware (Tier A); none → dev only
```

Pass `auth=build_auth(settings)` to `FastMCP(...)`. FastMCP then serves the protected-resource metadata,
emits the `WWW-Authenticate` challenge on 401, runs the proxy authorize/callback/token endpoints, and
validates bearer tokens. **`base_url` must be the real public HTTPS FQDN** or the advertised metadata and
redirect URIs point at the wrong host behind ACA ingress and the flow breaks.

**Lighter alternative — `AzureJWTVerifier`** (validate tokens only, no proxy/consent). Use when clients
obtain Entra tokens out-of-band (their own login, or a daemon using client-credentials):

```python
from fastmcp.server.auth.providers.azure import AzureJWTVerifier
auth = AzureJWTVerifier(tenant_id=..., client_id=..., required_scopes=["mcp.invoke"])
```

**Calling downstream Microsoft APIs as the user** — if a tool must hit Graph/Azure on the caller's behalf,
use the **On-Behalf-Of** flow (`EntraOBOToken`, same provider module) to exchange the inbound token for a
downstream one, preserving the user's identity and least privilege rather than using app-only permissions.

**Which to choose:**

| Situation | Use |
|---|---|
| Interactive client (Claude Desktop/Code), per-user identity, browser sign-in | **`AzureProvider`** (OAuth proxy) |
| Clients already obtain Entra tokens / daemon-to-daemon | **`AzureJWTVerifier`** |
| Internal machine-to-machine you fully control, fastest to ship | **API key** (Tier A) |
| Cutting over from key → OAuth without downtime | **`MultiAuth`** (both at once) |

> Non-Azure IdP? FastMCP ships equivalent providers for Auth0, Google, GitHub, WorkOS, Keycloak, Descope,
> Clerk, and others — the resource-server pattern is identical; only the provider class changes.

---

## 7. Secrets & identity — non-negotiables

- **Nothing secret in the image or in git.** Secrets live in Key Vault → ACA `secretRef` env, or are read at runtime via managed identity.
- **Managed identity for ACR pulls** (`AcrPull` role) — never `--registry-password`.
- Prefer **Key Vault references via managed identity** over copying secret *values* into Bicep params where possible.
- **Federated credentials over client secrets** for the Entra app in production (nothing to rotate or leak). A client secret is fine to start — put a rotation reminder on it.
- **Rotate** API keys and client secrets with overlapping validity for zero-downtime rotation.
- **Least privilege** on the managed identity: `AcrPull`, plus a scoped `Key Vault Secrets User` only if it reads the vault directly. Nothing broader.
- Enforce **HTTPS only** (`allowInsecure: false`) and **TLS 1.2+**. **Scrub query strings** from logs if you keep the `?api_key=` fallback.

---

## 8. CI/CD wired to GitHub (OIDC — no stored cloud creds)

GitHub Actions authenticates to Azure with a short-lived OIDC token — **zero long-lived cloud secrets in GitHub.**

```bash
# deploy identity, federated to your repo's main branch
DEPLOY_APP_ID=$(az ad app create --display-name "$APP-gha-deploy" --query appId -o tsv)
az ad sp create --id $DEPLOY_APP_ID
az ad app federated-credential create --id $DEPLOY_APP_ID --parameters '{
  "name": "gha-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<org>/<repo>:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
SP_OID=$(az ad sp show --id $DEPLOY_APP_ID --query id -o tsv)
az role assignment create --assignee-object-id $SP_OID --assignee-principal-type ServicePrincipal \
  --role Contributor --scope $(az group show -n $RG --query id -o tsv)

gh secret set AZURE_CLIENT_ID       -b "$DEPLOY_APP_ID"
gh secret set AZURE_TENANT_ID       -b "$(az account show --query tenantId -o tsv)"
gh secret set AZURE_SUBSCRIPTION_ID -b "$(az account show --query id -o tsv)"
```

```yaml
# .github/workflows/deploy.yml
name: deploy-mcp
on: { push: { branches: [main] } }
permissions: { id-token: write, contents: read }   # id-token: write is required for OIDC
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/login@v2
        with:
          client-id:       ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id:       ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - name: Build image in ACR
        run: az acr build -r ${{ vars.ACR }} -t ${{ vars.APP }}:${{ github.sha }} .
      - name: Deploy Bicep
        run: |
          ENV_ID=$(az containerapp env show -g ${{ vars.RG }} -n ${{ vars.ACA_ENV }} --query id -o tsv)
          az deployment group create -g ${{ vars.RG }} \
            --template-file deploy/main.bicep \
            --parameters appName=${{ vars.APP }} \
              containerImage=${{ vars.ACR }}.azurecr.io/${{ vars.APP }}:${{ github.sha }} \
              acaEnvId="$ENV_ID" authMode=oauth
```

- Pin images by **`github.sha`**, not `latest` — traceable revisions, and rollback is a redeploy of an old tag.
- Keep app **secrets out of the workflow**: OIDC handles auth; app secrets stay in Key Vault and are referenced by the deployment.
- Add a **PR job** (`az acr build` + `az bicep build` / `what-if`) that validates without deploying.

---

## 9. Client connection config

**API-key mode (header — preferred):**
```json
{ "mcpServers": { "my-mcp": {
  "type": "http",
  "url": "https://<fqdn>/mcp",
  "headers": { "x-api-key": "<key>" }
} } }
```

**API-key mode (query-string fallback, for clients that can't set headers):** `https://<fqdn>/mcp?api_key=<key>`

**OAuth mode:** point the client at `https://<fqdn>/mcp` with **no static credential** — a spec-compliant
client hits the `401`, discovers Entra via the protected-resource metadata, runs the browser sign-in, and
attaches the bearer token automatically.

**Smoke test:**
```bash
curl https://<fqdn>/health           # → OK   (always open)

curl -X POST 'https://<fqdn>/mcp' \
  -H 'x-api-key: <key>' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"smoke","version":"1"}}}'

# OAuth: confirm the challenge + metadata are well-formed
curl -i https://<fqdn>/mcp                                  # → 401 + WWW-Authenticate: Bearer resource_metadata="..."
curl https://<fqdn>/.well-known/oauth-protected-resource    # → JSON pointing at Entra
```

---

## 10. Observability, cost, hardening

- **Logs/metrics:** create the ACA env with a Log Analytics workspace (`--logs-destination log-analytics`) and stream `ContainerAppConsoleLogs_CL`. Add **Application Insights** for request tracing.
- **Cost:** ACA Consumption bills per vCPU-second + memory + requests; `minReplicas: 0` is cheapest for spiky/stateless servers (accept a cold start). ACR Basic + a small File share are a few dollars/month. Watch any per-call LLM/API spend inside your tools.
- **Hardening checklist:** non-root container · HTTPS only, TLS 1.2+ · secrets only via Key Vault/secretRef · managed identity, least privilege · `/health` is the *only* open route · per-client keys or OAuth scopes · query strings scrubbed from logs · image pinned by sha/digest · dependency + image scanning.

---

## 11. Common pitfalls (avoid these from day one)

1. **SSE is dead** — only run `transport="http"` (Streamable HTTP). Don't expose the legacy SSE endpoint.
2. **`/health` must be unauthenticated** or ACA probes fail and the revision never goes healthy.
3. **Startup probe too tight** kills slow-booting containers (model downloads, cache warmup). Size `periodSeconds × failureThreshold` against real cold-start time.
4. **Single-replica is a *consequence* of local state, not a default.** Stateless servers should scale out — don't copy `min=max=1` blindly.
5. **Embedded DB on a network share + multiple writers = corruption.** One writer, or use a managed DB and go stateless.
6. **`public_base_url`/`base_url` must equal the real ingress FQDN** in OAuth mode, or discovery metadata and redirect URIs point at the wrong host and auth breaks.
7. **Heavy synchronous work on the asyncio loop** blocks `/health` → liveness reaps the container mid-job. Offload to a thread or a separate ACA Job.
8. **Query-string secrets land in logs.** Header first; treat `?api_key=` / `?code=` as compatibility only and scrub them.
9. **Entra has no open DCR** — that's *why* you use `AzureProvider`'s OAuth proxy rather than expecting clients to self-register.
10. **Too many overlapping tools** degrade the model's routing. Prefer few, intent-named, well-documented tools.

---

### Appendix — quick decision tree

- *Stateless API-wrapper MCP, internal callers* → ACA stateless (`min:0`, scale out), **API key (header)**, no file share.
- *Stateless MCP needing state* → externalize to **Postgres / Cosmos / AI Search / Blob** via managed identity; stay scaled-out.
- *Stateful MCP (embedded DB/index)* → ACA + File share, **pin to 1 replica**, background work on a thread.
- *User-facing MCP (Claude Desktop/Code)* → **`AzureProvider` OAuth**, scope `mcp.invoke`, federated cred in prod.
- *MCP that calls Graph/Azure as the user* → OAuth + **`EntraOBOToken`** on-behalf-of.
- *Migrating key → OAuth without downtime* → **`MultiAuth`**, cut clients over, then drop the key tier.
