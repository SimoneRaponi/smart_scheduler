# Smart Scheduler

**The smart way to schedule smart working.**

More people than desks? Smart Scheduler turns the weekly who-works-from-where
negotiation into a single command. It collects everyone's wishes, fills the
office up to capacity and never beyond, and when not everyone can be
accommodated it chooses who concedes fairly, with a memory that pays
everyone back.

## What makes it smart

- **An optimizer, not a spreadsheet.** Plans are computed by a constraint
  solver (Google OR-Tools CP-SAT), not by first-come-first-served or by
  whoever asks loudest.
- **Wishes come first, provably.** A two-phase optimization guarantees that
  no request is ever sacrificed if it could have been satisfied. Fairness
  only decides *who* concedes when the math says someone must.
- **A long memory for fairness.** Every concession, of any kind, earns a
  credit. Credits persist between weeks and buy priority the next time a
  sacrifice is needed, so the burden rotates instead of always falling on
  the same person.
- **Hard where it must be, soft where it matters.** Desk capacity,
  mandatory office days and the company-wide weekly cap are non-negotiable.
  Everything personal, including desired days, minimum targets, or personal
  maximums, is a request the scheduler honors when it can and compensates
  when it can't.
- **Nothing up its sleeve.** Every run prints who got what, who conceded,
  why, and the credits that will repay them, so the whole team can verify
  the plan is fair.

## Quick start

Requires Python 3.9+.

    pip install ortools pyyaml
    python scheduler.py config.yaml

Each run prints the weekly person-by-day table, exports a CSV
(`plan_<year>-w<week>.csv`) to share with the team, and updates the fairness
credits in `state.json`.

Useful options:

    python scheduler.py config.yaml --dry-run    # preview without touching credits
    python scheduler.py config.yaml --seed 42    # reproducible plan

## Configuration

Everything lives in one YAML file:

```yaml
max_smart_per_week: 2   # company-wide weekly cap (hard: never exceeded)
min_smart_per_week: 0   # remote-days target per person (soft, default 0)
desks: 6                # desks available (hard)
state_file: state.json

members:
  - name: Ada
    desired_smart_days: [mon, fri]   # what she'd like
  - name: Grace
    mandatory_office_days: [tue]     # where she must be (hard)
  - name: Alan
    max_smart_days: 0                # prefers the office; can still be asked
                                     # to stay home (never beyond the company
                                     # cap), earning credit
  - name: Edsger
    min_smart_days: 2                # should get two remote days
```

Valid days are `mon` through `fri`. Per-person `min_smart_days` and
`max_smart_days` override the global values but must stay within the
company-wide cap. Set `min_smart_per_week: 2` together with
`max_smart_per_week: 2` to express "everyone should take exactly two
remote days".

## How fairness works

The solver runs in two phases. Phase 1 maximizes the number of accommodated
request units: desired days granted, minimum targets reached, forced days
avoided. Phase 2 re-optimizes within that optimum, so credits decide *who*
concedes but can never reduce *how much* is accommodated overall.

A concession is any of: a desired day refused, a remote day missing toward
someone's target, or a remote day forced above someone's personal
maximum, needed when people who prefer the office would otherwise leave no
desks for those obliged to come in. Forcing never crosses the company-wide
cap: that line is hard for everyone. Each concession is worth one credit; overlapping
grievances count once. Credits multiply the weight of a person's requests in
phase 2, so last week's concession becomes this week's priority, and a small
week-seeded random noise breaks ties so equal cases don't always resolve the
same way. When all of a person's requests are met, their credit resets.

Delete `state.json` to start fresh (for example after a team change).

## Built-in sanity checks

Before solving, the scheduler validates the configuration (unknown keys,
invalid days, contradictory limits) and explains upfront any concession the
numbers make inevitable, such as a target that cannot fit around mandatory
office days, or maximums too low to cover the desk shortage. Blocking errors are reserved
for genuinely impossible weeks: a day on which mandatory office presence
leaves fewer people available for remote work than the desk shortage
requires, or a desk shortage that not even everyone at the company-wide cap
could absorb. In both cases it tells you exactly what is impossible and why,
instead of failing obscurely.

## License

Released under the [MIT License](LICENSE).
