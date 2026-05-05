using './main.bicep'

param environmentName = readEnvironmentVariable('AZURE_ENV_NAME', 'fabric-mcp-dev')
param location = readEnvironmentVariable('AZURE_LOCATION', 'germanywestcentral')
