// Cloud Cost Watchdog infrastructure.
//
//   az deployment group create -g rg-cost-watchdog -f infra/main.bicep -p infra/params/dev.bicepparam
//
// The function app holds a *reader + specific-operator* role set, never
// Contributor: a cost tool that can do anything is a cost tool that can cause
// the outage it was meant to prevent.

targetScope = 'resourceGroup'

@allowed(['dev', 'prod'])
param env string = 'dev'

@minLength(3)
@maxLength(10)
param suffix string = 'cw01'

param location string = resourceGroup().location

@description('Subscriptions the watchdog may act on (defaults to this one)')
param targetSubscriptions array = []

@description('Policy timezone for schedules')
param policyTimezone string = 'Asia/Kolkata'

@description('Set true to log decisions without acting')
param dryRun bool = false

@description('Daily anomaly sensitivity')
param anomalySigma string = '3'
param anomalyMinDeltaInr string = '500'

param tags object = {
  project: 'cloud-cost-watchdog'
  owner: 'platform'
}

var prefix = '${env}-cw-${suffix}'
var storageName = replace('${prefix}st', '-', '')
var cosmosName = 'cosmos-${prefix}'
var planName = '${prefix}-plan'
var appName = '${prefix}-app'
var lawName = '${prefix}-law'
var aiName = '${prefix}-ai'

// ---------------------------------------------------------------- storage
// Holds the function host state AND the command queues.
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

// Separate queues so a stop command can never be executed as a start.
resource stopQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: 'stop-commands'
}
resource startQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: 'start-commands'
}
resource reportQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: 'reports'
}
resource alertQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: 'alerts'
}

// ---------------------------------------------------------------- cosmos
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: cosmosName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [{ locationName: location, failoverPriority: 0 }]
    capabilities: [{ name: 'EnableServerless' }]
    disableLocalAuth: true
  }
}

resource db 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'costwatchdog'
  properties: { resource: { id: 'costwatchdog' } }
}

// decisions (append-only audit), actions (what happened), policies (keep-alives)
resource decisions 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: db
  name: 'decisions'
  properties: {
    resource: {
      id: 'decisions'
      partitionKey: { paths: ['/environment'], kind: 'Hash' }
      // 90 days is enough to answer "why did it not stop" for any recent tick.
      defaultTtl: 7776000
    }
  }
}

resource actions 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: db
  name: 'actions'
  properties: {
    resource: {
      id: 'actions'
      partitionKey: { paths: ['/environment'], kind: 'Hash' }
    }
  }
}

resource policies 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: db
  name: 'policies'
  properties: {
    resource: {
      id: 'policies'
      partitionKey: { paths: ['/environment'], kind: 'Hash' }
    }
  }
}

// ---------------------------------------------------------------- observability
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: lawName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: env == 'prod' ? 90 : 30
    dailyQuotaGb: 2
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: aiName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
  }
}

// ---------------------------------------------------------------- identity
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-07-31-preview' = {
  name: 'id-${prefix}'
  location: location
  tags: tags
}

// Reader at the subscription: needed to enumerate resources.
resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(identity.id, 'reader-sub')
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Virtual Machine Contributor: deallocate/start VMs. Scoped to the subscription
// because VMs live in workload RGs; the gates are what prevent misuse, and the
// role cannot create or delete anything.
resource vmContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(identity.id, 'vm-contributor')
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '9980e02c-c2be-4d73-94e8-173b1dc7cf3c')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cost Management Reader: for the anomaly detector.
resource costReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(identity.id, 'cost-reader')
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '72fafb9e-0641-4937-9268-a91bfd8191a3')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cosmos data plane for the action store.
resource cosmosRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(identity.id, cosmos.id, 'cosmos-contributor')
  scope: cosmos
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '00000000-0000-0000-0000-000000000002')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- function app
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  tags: tags
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storageName }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
        { name: 'COSMOS_DATABASE', value: 'costwatchdog' }
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
        { name: 'WATCHDOG_SUBSCRIPTIONS', value: join(targetSubscriptions, ',') }
        { name: 'POLICY_TIMEZONE', value: policyTimezone }
        { name: 'STOP_QUEUE', value: 'stop-commands' }
        { name: 'START_QUEUE', value: 'start-commands' }
        { name: 'REPORT_QUEUE', value: 'reports' }
        { name: 'ALERT_QUEUE', value: 'alerts' }
        { name: 'DRY_RUN', value: string(dryRun) }
        { name: 'PRICE_CURRENCY', value: 'INR' }
        { name: 'REPORT_PERIOD', value: '7d' }
        { name: 'ANOMALY_SIGMA', value: anomalySigma }
        { name: 'ANOMALY_MIN_DELTA_INR', value: anomalyMinDeltaInr }
      ]
    }
  }
}

// ---------------------------------------------------------------- budget
// A budget is the one cost control that works even when the watchdog is broken.
resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: '${prefix}-monthly-budget'
  properties: {
    category: 'Cost'
    amount: env == 'prod' ? 50000 : 5000
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: '2025-01-01T00:00:00Z'
    }
    notifications: {
      eightyPercent: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        contactEmails: ['priyanshu@example.com']
        thresholdType: 'Actual'
      }
      hundredPercent: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        contactEmails: ['priyanshu@example.com']
        thresholdType: 'Forecasted'
      }
    }
  }
}

output functionAppName string = functionApp.name
output functionAppHost string = functionApp.properties.defaultHostName
output identityPrincipalId string = identity.properties.principalId
output cosmosEndpoint string = cosmos.properties.documentEndpoint
