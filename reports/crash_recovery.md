# Crash-recovery test — public_02_incidents

Kills (target sim minute → actual sim minute when SIGKILL landed): 1d10:15→1d11:00, 2d10:45→2d11:00, 3d12:15→3d13:00, 3d14:30→3d15:00, 5d15:15→5d16:00, 5d16:00→5d17:00, 7d11:30→7d12:00

Governance actions: reference 44, crashed run 44.

- PASS — finished the scenario
- PASS — no approval applied twice
- PASS — no proposal_id reused
- PASS — zero apply conflicts
- PASS — governance actions identical to uninterrupted run

Restart/reconcile records from the controller's own audit store:

```
2027-01-05T12:00:00+05:30|Controller restarted at D1 12:00; resumed from chunk 18
2027-01-06T12:00:00+05:30|Controller restarted at D2 12:00; resumed from chunk 32
2027-01-07T15:00:00+05:30|Controller restarted at D3 15:00; resumed from chunk 47
2027-01-09T17:00:00+05:30|Controller restarted at D5 17:00; resumed from chunk 76
2027-01-11T12:00:00+05:30|Controller restarted at D7 12:00; resumed from chunk 97
```
