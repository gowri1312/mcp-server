@description('Name of the Container App and associated resources')
param appName string = 'acgowri001'

@description('Azure region for all resources')
param location string = 'australiasoutheast'

@description('Full image reference, e.g. acrgowri001.azurecr.io/acgowri001:<sha>')
param containerImage string

@description('Resource ID of the Container Apps environment')
param acaEnvId string

@description('Resource ID of the Azure Container Registry')
param acrId string


@description('MCP API key — pass from Key Vault; never hard-code')
@secure()
param mcpApiKey string

@description('Client ID of the user-assigned managed identity (output from identity module or pre-created)')
param managedIdentityClientId string

@description('Principal ID of the user-assigned managed identity')
param managedIdentityPrincipalId string

@description('Resource ID of the user-assigned managed identity')
param managedIdentityId string

@description('Azure SQL fully-qualified server name')
param sqlServer string

@description('Azure SQL database name')
param sqlDatabase string

@description('Azure Storage account name')
param storageAccountName string

// ── Role definition IDs (built-in) ───────────────────────────────────────────
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// ── AcrPull on the registry ───────────────────────────────────────────────────
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acrId, managedIdentityPrincipalId, acrPullRoleId)
  scope: resourceGroup()   // scoped to RG; tighten to ACR resource if preferred
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}


// ── Container App ─────────────────────────────────────────────────────────────
resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${managedIdentityId}': {} }
  }
  properties: {
    managedEnvironmentId: acaEnvId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      secrets: [
        { name: 'mcp-api-key', value: mcpApiKey }
      ]
      registries: [
        { server: split(containerImage, '/')[0], identity: managedIdentityId }
      ]
    }
    template: {
      containers: [
        {
          name: appName
          image: containerImage
          env: [
            { name: 'PORT',                          value: '8000' }
            { name: 'AUTH_MODE',                     value: 'apikey' }
            { name: 'MCP_API_KEY',                   secretRef: 'mcp-api-key' }
            { name: 'MANAGED_IDENTITY_CLIENT_ID',    value: managedIdentityClientId }
            { name: 'SQL_SERVER',                    value: sqlServer }
            { name: 'SQL_DATABASE',                  value: sqlDatabase }
            { name: 'STORAGE_ACCOUNT_NAME',          value: storageAccountName }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: { port: 8000, path: '/health' }
              periodSeconds: 15
              failureThreshold: 20   // 5 min total startup grace
            }
            {
              type: 'Liveness'
              httpGet: { port: 8000, path: '/health' }
              periodSeconds: 30
              failureThreshold: 10
            }
          ]
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      // Stateless: scale to zero when idle, up to 5 replicas under load
      scale: { minReplicas: 0, maxReplicas: 5 }
    }
  }
  dependsOn: [acrPull]
}

output mcpUrl string = 'https://${app.properties.configuration.ingress.fqdn}/mcp'
output healthUrl string = 'https://${app.properties.configuration.ingress.fqdn}/health'
