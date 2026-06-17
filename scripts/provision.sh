#!/usr/bin/env bash
# One-time Azure provisioning script.
# Run this ONCE before the first deployment. Idempotent — safe to re-run.
# Prerequisites: az CLI ≥ 2.60, gh CLI, Owner/Contributor+UAA on the subscription.

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
SUBSCRIPTION="c068714d-e7fb-4382-8069-8f3d791a3a5f"
RG="rg-gowri-mcp-002"
LOC="australiasoutheast"
ACR="acrgowri001"
ACA_ENV="cae-acgowri001"
KV="kv-acgowri001"         # Key Vault name — globally unique, max 24 chars
APP="acgowri001"
GH_REPO="gowri1312/mcp-server"

# Your Azure SQL server + database (must already exist)
SQL_SERVER="sql-drmexp-dev-ase-portal-001.database.windows.net"
SQL_DATABASE="db-drmexp-dev-ase-portal-001"

# Your Azure Storage account (must already exist)
STORAGE_ACCOUNT="gowristorageac01"

# ── Login & set subscription ───────────────────────────────────────────────────
az account set --subscription "$SUBSCRIPTION"
echo "Using subscription: $(az account show --query name -o tsv)"

# ── Resource group ─────────────────────────────────────────────────────────────
az group create -n "$RG" -l "$LOC" -o none
echo "✓ Resource group: $RG"

# ── Container Registry ─────────────────────────────────────────────────────────
az acr create -g "$RG" -n "$ACR" --sku Basic -l "$LOC" -o none
echo "✓ ACR: $ACR"

# ── Container Apps environment ─────────────────────────────────────────────────
az containerapp env create -g "$RG" -n "$ACA_ENV" -l "$LOC" --logs-destination none -o none
echo "✓ ACA environment: $ACA_ENV"

# ── Key Vault ──────────────────────────────────────────────────────────────────
az keyvault create -g "$RG" -n "$KV" -l "$LOC" --enable-rbac-authorization true -o none
echo "✓ Key Vault: $KV"

# Store secrets
MCP_API_KEY=$(openssl rand -hex 32)
az keyvault secret set --vault-name "$KV" --name mcp-api-key --value "$MCP_API_KEY" -o none
echo "✓ Secret stored: mcp-api-key"
echo "  MCP_API_KEY=$MCP_API_KEY  ← save this for client config"

# ── User-assigned managed identity ────────────────────────────────────────────
az identity create -g "$RG" -n "${APP}-id" -l "$LOC" -o none
echo "✓ Managed identity: ${APP}-id"

# Grant Key Vault Secrets User so the container can read secrets directly if needed
UAI_PRINCIPAL=$(az identity show -g "$RG" -n "${APP}-id" --query principalId -o tsv)
KV_ID=$(az keyvault show -n "$KV" --query id -o tsv)
az role assignment create \
  --assignee-object-id "$UAI_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope "$KV_ID" -o none
echo "✓ Key Vault Secrets User granted to identity"

# Grant db_datareader in Azure SQL (run this SQL as admin):
echo ""
echo "──────────────────────────────────────────────────────────"
echo "ACTION REQUIRED: Grant SQL access to the managed identity."
echo "Connect to $SQL_DATABASE on $SQL_SERVER as admin and run:"
echo ""
UAI_NAME="${APP}-id"
echo "  CREATE USER [${UAI_NAME}] FROM EXTERNAL PROVIDER;"
echo "  ALTER ROLE db_datareader ADD MEMBER [${UAI_NAME}];"
echo "──────────────────────────────────────────────────────────"
echo ""

# ── GitHub OIDC federated credential for CI/CD ────────────────────────────────
DEPLOY_APP_ID=$(az ad app create --display-name "${APP}-gha-deploy" --query appId -o tsv)
az ad sp create --id "$DEPLOY_APP_ID" -o none

az ad app federated-credential create --id "$DEPLOY_APP_ID" --parameters "{
  \"name\": \"gha-main\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:${GH_REPO}:ref:refs/heads/main\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}" -o none

SP_OID=$(az ad sp show --id "$DEPLOY_APP_ID" --query id -o tsv)
RG_ID=$(az group show -n "$RG" --query id -o tsv)
az role assignment create \
  --assignee-object-id "$SP_OID" \
  --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope "$RG_ID" -o none
echo "✓ GitHub OIDC deploy identity created"

TENANT_ID=$(az account show --query tenantId -o tsv)
gh secret set AZURE_CLIENT_ID       -R "$GH_REPO" -b "$DEPLOY_APP_ID"
gh secret set AZURE_TENANT_ID       -R "$GH_REPO" -b "$TENANT_ID"
gh secret set AZURE_SUBSCRIPTION_ID -R "$GH_REPO" -b "$SUBSCRIPTION"
gh secret set KV_NAME               -R "$GH_REPO" -b "$KV"
gh secret set SQL_SERVER            -R "$GH_REPO" -b "$SQL_SERVER"
gh secret set SQL_DATABASE          -R "$GH_REPO" -b "$SQL_DATABASE"
gh secret set STORAGE_ACCOUNT_NAME  -R "$GH_REPO" -b "$STORAGE_ACCOUNT"
echo "✓ GitHub secrets set"

echo ""
echo "══════════════════════════════════════════════════════════"
echo "Provisioning complete. Next step: push to main to deploy."
echo ""
echo "Client config (API key):"
echo '{ "mcpServers": { "gowri-mcp": {'
echo '    "type": "http",'
echo '    "url": "https://<fqdn>/mcp",'
echo '    "headers": { "x-api-key": "'"$MCP_API_KEY"'" }'
echo '} } }'
echo "══════════════════════════════════════════════════════════"
