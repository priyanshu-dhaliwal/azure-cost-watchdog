# Measured savings — method and results

A savings number without a stated method is a guess with a currency symbol. This
document states the method, the assumptions, and where the number is *not*
reliable.

## Method

```
saving(resource) = retail_hourly_price(resource) × stopped_hours(resource)
```

- **`stopped_hours`** is measured from the action log: the gap between a `stop`
  action and the following `start`. It is **not** derived from the schedule,
  because the schedule says what should have happened and the log says what did.
  A resource still stopped at the end of the window counts up to the window end.
- **`retail_hourly_price`** comes from the Azure Retail Prices API for the
  resource's SKU and region, pay-as-you-go, Linux, no spot. Monthly units are
  divided by 730.
- The currency is whatever `PRICE_CURRENCY` is set to (default `INR`).

## What this number is, and is not

**It is:** "at list price, the hours this resource did not run would have cost
this much."

**It is not:**

- **A reduction in your invoice.** Reserved instances, savings plans, hybrid
  benefit and EA discounts all change the effective rate. If you run dev VMs
  under a 3-year reservation, deallocating them saves the *reservation* value,
  not the retail rate — and the reservation is already paid.
- **A saving for anything that was not fully stopped.** A stopped App Service
  app still pays for its plan; the report prices the app's compute but the plan
  remains. Partial-savings resources are flagged in the per-resource table.
- **Net of the watchdog's own cost.** The watchdog costs ~₹900/month; the report
  prints gross savings and the README states the net.

## Reference results (dev estate, 30 days)

34 resources opted in, standard schedule `mon-fri 09:00-19:00`.

| Resource type | Count | Stopped hours (30d) | Retail saving |
|---|---|---|---|
| VMs (Standard_B2s / D2s_v5) | 18 | 3,540 | ₹28,320 |
| AKS default node pools | 4 | 2,940 | ₹11,760 |
| App Service apps (plan still billed) | 9 | 4,050 | ₹0 * |
| SQL serverless databases | 3 | 2,700 | ₹2,180 |
| **Total** | **34** | **13,230** | **₹42,260** |

\* App Service plans are billed regardless of whether the app is running, so
stopping the app saves nothing. It is included because the watchdog *acts* on it
and the report must not pretend the action was free. The honest fix is to scale
the plan, which requires the Isolated tier or a containerised app — noted in the
README's "what I would do differently".

## Sanity checks built into the report

1. **Stopped hours cannot exceed the window.** A resource stopped for more hours
   than exist in the period indicates a missing `start` action; the report
   counts it but the anomaly detector's `topContributors` will show it.
2. **Unpriced resources are counted, not ignored.** `unpricedResources` in the
   totals is non-zero whenever a SKU could not be resolved; a savings figure that
   silently drops those is overstated.
3. **The action log is append-only.** A saving cannot be created by editing
   history.

## How to verify a single number by hand

```bash
# 1. the action log for one resource
az cosmosdb sql query --account-name cosmos-dev-cw-cw01 \
  --database-name costwatchdog --container-name actions \
  --query-text "SELECT * FROM c WHERE c.resourceName = 'dev-vm-jumpbox'"

# 2. the retail price for its SKU
curl -s "https://prices.azure.com/api/retail/prices?\$filter=serviceName eq 'Virtual Machines' and armRegionName eq 'centralindia' and armSkuName eq 'Standard_B2s'" \
  | jq '.Items[0].retailPrice'

# 3. multiply the stopped hours by the hourly price and compare
```

If step 3 does not match the report, the report is wrong and this document is
wrong with it — that is the point of writing the method down.
