@maxLength(20)
@minLength(4)
@description('Used to generate names for all resources in this file')
param resourceBaseName string

@maxLength(42)
param botDisplayName string

param botServiceName string = resourceBaseName
param botServiceSku string = 'F0'
param identityResourceId string
param identityClientId string
param identityTenantId string
param botAppDomain string

// OAuth connection parameters for SSO (delegated Graph access)
@secure()
param graphClientId string = ''
@secure()
param graphClientSecret string = ''
param graphTenantId string = ''

// Register your web service as a bot with the Bot Framework
resource botService 'Microsoft.BotService/botServices@2021-03-01' = {
  kind: 'azurebot'
  location: 'global'
  name: botServiceName
  properties: {
    displayName: botDisplayName
    endpoint: 'https://${botAppDomain}/api/messages'
    msaAppId: identityClientId
    msaAppMSIResourceId: identityResourceId
    msaAppTenantId:identityTenantId
    msaAppType:'UserAssignedMSI'
  }
  sku: {
    name: botServiceSku
  }
}

// Connect the bot service to Microsoft Teams
resource botServiceMsTeamsChannel 'Microsoft.BotService/botServices/channels@2021-03-01' = {
  parent: botService
  location: 'global'
  name: 'MsTeamsChannel'
  properties: {
    channelName: 'MsTeamsChannel'
  }
}

// OAuth connection for SSO - enables delegated Graph access via Bot Framework Token Service
resource botOAuthConnection 'Microsoft.BotService/botServices/connections@2022-09-15' = if (!empty(graphClientId)) {
  parent: botService
  name: 'graph'
  location: 'global'
  properties: {
    clientId: graphClientId
    clientSecret: graphClientSecret
    scopes: 'User.Read Mail.Read Files.Read.All Sites.Read.All openid profile offline_access'
    serviceProviderId: '30dd229c-58e3-4a48-bdfd-91ec48eb906c' // Azure Active Directory v2
    serviceProviderDisplayName: 'Azure Active Directory v2'
    parameters: [
      { key: 'tenantID', value: empty(graphTenantId) ? 'common' : graphTenantId }
      { key: 'tokenExchangeUrl', value: 'api://${botAppDomain}/${graphClientId}' }
    ]
  }
}
