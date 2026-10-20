using '../main.bicep'

env = 'dev'
suffix = 'cw01'
location = 'centralindia'
policyTimezone = 'Asia/Kolkata'
dryRun = true
targetSubscriptions = []
anomalySigma = '3'
anomalyMinDeltaInr = '200'
tags = {
  project: 'cloud-cost-watchdog'
  owner: 'platform-team'
  environment: 'dev'
  costCentre: 'platform-001'
  dataClassification: 'internal'
}
