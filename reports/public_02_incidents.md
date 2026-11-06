# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.361 | +53.2% | 0.254 | +40.1% | 0.486 | +3.8% | 30% | 132 (348) |
| nw_kyc | 0.441 | +17.9% | 0.279 | +19.1% | 0.595 | -2.2% | 0% | 24 (29) |
| nw_renewal | 0.488 | +12.8% | 0.291 | +10.9% | 0.559 | +0.8% | 5% | 57 (54) |
| ALL | 0.420 | +27.1% | 0.271 | +23.7% | 0.534 | +0.4% | 19% | 213 (431) |

## Changes applied by the controller

22 applied — neutral: 9, beneficial: 12, harmful: 1

- ks_cancel v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.018 → **neutral**
- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.018 → **neutral**
- ks_cancel v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.011 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.016 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.042 → **beneficial**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.032 → **beneficial**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.053 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_approved) dims=['voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nw_renewal v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.013 → **neutral**
- ks_cancel v6 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.174 → **beneficial**
- ks_cancel v8 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.112 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.281 → **beneficial**
- nw_renewal v7 (controller_approved) dims=['voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- ks_cancel v11 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=-0.007 didΔ=+0.000 → **neutral**
- ks_cancel v12 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nw_kyc v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.140 → **beneficial**
- ks_cancel v13 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.060 → **harmful**
- ks_cancel v14 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.063 → **beneficial**

## Governance

```
{
  "approval_requests": 21,
  "decisions": {
    "approved": 18,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 9,
  "auto_applied": 13,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "ks_cancel": 1,
    "nw_kyc": 1
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.099,
      "3": 0.0,
      "4": 0.0,
      "5": 0.0,
      "6": 0.0,
      "7": 0.0,
      "8": 0.0,
      "9": 0.033
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 1530,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "2": 0.145,
    "3": 0.152,
    "4": 0.142,
    "5": 0.157,
    "6": 0.145,
    "7": 0.147,
    "8": 0.154,
    "9": 0.151
  }
}
```

Credits used: 3 · validation runs: 1 · LLM fix calls: 0