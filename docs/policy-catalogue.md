# Policy catalogue

Policies do the work that auto-shutdown cannot: they stop waste from being
created in the first place. Auto-shutdown saves the *running* cost of dev
machines; policy prevents an oversized SKU from ever existing.

Each entry states its effect, its scope, and its blast radius — because a `Deny`
policy applied to the wrong management group stops every deployment, including
the one that would fix it.

## 1. `deny-untagged-vm` — Deny

**Scope:** Landing Zones management group
**Rule:** a VM or disk without `autoShutdown` and `environment` tags is denied.

**Why:** a resource that is not tagged is a resource the watchdog cannot see.
Denying creation is the only way to keep the tag coverage at 100% without a
weekly cleanup job.

**Blast radius:** a team that genuinely cannot tag a VM is blocked. Mitigation:
the tag values are documented in the platform README, and `owner` is already
required by the landing zone baseline.

**Exemption:** resources in the `Sandbox` management group are audited, not
denied.

## 2. `deny-expensive-sku-in-dev` — Deny

**Scope:** subscriptions tagged `environment=dev`
**Rule:** denies VM sizes in the `E*`, `M*`, `L*` series and GPU families
(`NC*`, `ND*`, `NV*`), and any SQL tier above `S1`.

**Why:** the single biggest dev cost overrun is not a forgotten VM — it is a
correctly-sized-for-prod SKU left in a dev subscription for a week. A D2 costs
~₹7/hour; an E16 costs ~₹90/hour.

**Blast radius:** a legitimate dev workload that needs a large SKU is blocked.
The exception path is a tag `skuException=true` plus an approval on the PR that
adds it — deliberately friction-heavy, because the alternative is silence.

## 3. `auto-tag-from-rg` — Modify

**Scope:** all subscriptions
**Rule:** copies `environment`, `costCentre` and `owner` from the resource group
onto any resource missing them.

**Why:** makes `deny-untagged-vm` survivable. Tag inheritance is not a thing in
Azure; this policy *is* the inheritance.

**Blast radius:** low — Modify only adds tags. It requires a managed identity
with Tag Contributor.

## 4. `deny-public-ip-in-dev` — Deny

**Scope:** subscriptions tagged `environment=dev`
**Rule:** denies `Microsoft.Network/publicIPAddresses` with a Standard SKU
unless the name is prefixed `pip-bastion` or `pip-fw`.

**Why:** a public IP in dev is almost always a debugging leftover, and it is also
an attack surface. It is both a cost and a security policy.

## 5. `budget-action-group` — (not a policy)

A subscription budget at 80% actual / 100% forecasted, wired to the same action
group as the anomaly detector. This is not a policy but it belongs in the
catalogue: it is the control that still works when the watchdog itself is broken.

---

## Rollout order (important)

1. `auto-tag-from-rg` (Modify) — safe, adds tags
2. `deny-untagged-vm` in `Audit` for two weeks — see what would break
3. `deny-untagged-vm` in `Deny`
4. `deny-expensive-sku-in-dev` in `Audit`, then `Deny`
5. `deny-public-ip-in-dev` in `Audit`, then `Deny`

Applying the deny set first stops deployments across the estate, including the
deployment that would tag the offending resources. The two-phase rollout is the
difference between a policy and an incident.
