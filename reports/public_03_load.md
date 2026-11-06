# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.392 | +16.2% | 0.264 | +8.7% | 0.528 | +6.3% | 41% | 437 (409) |
| arav_kyc_1 | 0.425 | +18.4% | 0.251 | +16.5% | 0.592 | -1.4% | 0% | 396 (352) |
| arav_renewal_0 | 0.479 | +18.2% | 0.287 | +8.6% | 0.590 | +7.6% | 41% | 497 (408) |
| kest_cancel_2 | 0.393 | +16.6% | 0.264 | +8.8% | 0.528 | +5.8% | 40% | 428 (418) |
| kest_kyc_1 | 0.474 | +33.2% | 0.275 | +28.1% | 0.596 | -0.2% | 0% | 483 (370) |
| kest_renewal_0 | 0.465 | +15.7% | 0.285 | +7.8% | 0.577 | +5.9% | 28% | 446 (403) |
| nort_cancel_2 | 0.401 | +18.1% | 0.266 | +8.9% | 0.532 | +6.7% | 46% | 410 (368) |
| nort_kyc_1 | 0.437 | +22.8% | 0.256 | +18.9% | 0.594 | -0.3% | 0% | 406 (318) |
| nort_renewal_0 | 0.482 | +19.0% | 0.288 | +9.0% | 0.592 | +8.1% | 43% | 453 (419) |
| tide_cancel_2 | 0.395 | +15.2% | 0.265 | +8.4% | 0.532 | +6.2% | 46% | 453 (401) |
| tide_kyc_1 | 0.477 | +33.9% | 0.276 | +28.3% | 0.594 | -0.6% | 0% | 429 (376) |
| tide_renewal_0 | 0.470 | +16.1% | 0.287 | +8.8% | 0.577 | +5.8% | 32% | 438 (425) |
| ALL | 0.441 | +20.2% | 0.272 | +12.9% | 0.570 | +4.4% | 25% | 5276 (4667) |

## Changes applied by the controller

62 applied — neutral: 24, beneficial: 38

- arav_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- arav_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- kest_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- kest_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- nort_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- nort_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- tide_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- tide_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- arav_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- arav_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.015 → **neutral**
- kest_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- kest_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.015 → **neutral**
- nort_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- nort_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.015 → **neutral**
- tide_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- tide_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.015 → **neutral**
- arav_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- kest_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- nort_cancel_2 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.002 didΔ=+0.004 → **neutral**
- nort_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- tide_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.043 → **beneficial**
- arav_cancel_2 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.002 didΔ=+0.004 → **neutral**
- kest_cancel_2 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nort_cancel_2 v5 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.002 didΔ=+0.004 → **neutral**
- kest_cancel_2 v5 (controller_auto) dims=['welcome_message'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.058 → **beneficial**
- arav_cancel_2 v5 (controller_auto) dims=['welcome_message'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.060 → **beneficial**
- nort_cancel_2 v6 (controller_auto) dims=['welcome_message'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v5 (controller_auto) dims=['welcome_message'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v6 (controller_approved) dims=['voice'] engageΔ=+0.056 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.066 → **beneficial**
- nort_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v6 (controller_approved) dims=['voice'] engageΔ=+0.055 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v7 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_cancel_2 v8 (controller_approved) dims=['voice'] engageΔ=+0.056 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.086 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.086 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.065 → **beneficial**
- kest_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.535 → **beneficial**
- tide_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.535 → **beneficial**
- kest_cancel_2 v6 (controller_approved) dims=['voice'] engageΔ=+0.055 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v9 (controller_approved) dims=['cohorts', 'welcome_message', 'schedule'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.085 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.001 → **neutral**
- arav_renewal_0 v7 (controller_approved) dims=['voice'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v7 (controller_approved) dims=['voice'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.086 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v7 (controller_approved) dims=['voice'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.543 → **beneficial**
- kest_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- nort_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.541 → **beneficial**
- kest_cancel_2 v7 (controller_approved) dims=['cohorts', 'welcome_message', 'schedule'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.003 → **neutral**
- arav_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.003 → **neutral**
- tide_cancel_2 v7 (controller_approved) dims=['cohorts', 'welcome_message', 'schedule'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v8 (controller_approved) dims=['cohorts', 'welcome_message', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_renewal_0 v8 (controller_approved) dims=['cohorts', 'voice', 'schedule'] engageΔ=-0.009 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 43,
  "decisions": {
    "approved": 37,
    "rejected": 6,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 6
  },
  "applied_via_approval": 36,
  "auto_applied": 26,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "arav_cancel_2": 2,
    "arav_renewal_0": 2,
    "nort_cancel_2": 1,
    "nort_renewal_0": 2,
    "tide_cancel_2": 2,
    "tide_renewal_0": 2,
    "arav_kyc_1": 1,
    "kest_cancel_2": 1,
    "kest_renewal_0": 1,
    "nort_kyc_1": 1,
    "tide_kyc_1": 1,
    "kest_kyc_1": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.804,
    "4": 0.8
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.8,
    "4": 0.799
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.797,
    "4": 0.8
  },
  "tide_renewal_0:welcome_message=W3": {
    "3": 0.8,
    "4": 0.798
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0