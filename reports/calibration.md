# Stopping-rule calibration (2000 sims, 90 connected calls/hour, hourly looks, 7 days)

Rule: promote at z >= 3.0 with >= 400 connected calls on the arm; drop at z <= -2.0 with >= 150. Challenger share 15% each.

| world | P(any arm promoted) | P(true winner promoted) | median hours to promote | P(null arm wrongly dropped) |
|---|---|---|---|---|
| A/A, 3 challengers (false winners) | 0.021 | 0.000 | — | 0.109 |
| A/A, 2 challengers | 0.015 | 0.000 | — | 0.114 |
| one arm +10%, two null | 0.729 | 0.722 | 48.0 | 0.102 |
| one arm +18%, one null | 1.000 | 0.998 | 31.0 | 0.073 |
| one arm -10% (harm), two null | 0.015 | 0.000 | — | 0.112 |

False-winner rate (A/A, 3 challengers) by promote threshold:

| z_promote | FWER |
|---|---|
| 2.5 | 0.077 |
| 2.75 | 0.043 |
| 3.0 | 0.023 |
| 3.25 | 0.011 |
