using '../main.bicep'

env = 'prod'
suffix = 'cw01'
location = 'centralindia'
policyTimezone = 'Asia/Kolkata'
dryRun = false
targetSubscriptions = []
anomalySigma = '3'
anomalyMinDeltaInr = '500'
tags = {
  project: 'cloud-cost-watchdog'
  owner: 'platform-team'
  environment: 'prod'
  costCentre: 'platform-001'
  dataClassification: 'internal'
}
