targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment')
param environmentName string

@minLength(1)
@description('Primary location for all resources')
param location string

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    location: location
    tags: tags
    logAnalyticsName: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    applicationInsightsName: '${abbrs.insightsComponents}${resourceToken}'
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    location: location
    tags: tags
    storageName: '${abbrs.storageStorageAccounts}${resourceToken}'
  }
}

module functionApp './modules/function.bicep' = {
  name: 'functionApp'
  scope: rg
  params: {
    location: location
    tags: tags
    functionAppName: '${abbrs.webSitesFunctions}${resourceToken}'
    storageAccountName: storage.outputs.storageName
    applicationInsightsConnectionString: monitoring.outputs.applicationInsightsConnectionString
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_FUNCTION_APP_NAME string = functionApp.outputs.functionAppName
output AZURE_FUNCTION_APP_URL string = functionApp.outputs.functionAppUrl
output SERVICE_API_ENDPOINTS array = ['${functionApp.outputs.functionAppUrl}/api/health', '${functionApp.outputs.functionAppUrl}/api/openapi.json']
