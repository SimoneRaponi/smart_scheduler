#!/usr/bin/env python3
"""Smart Scheduler: weekly smart-working planner with long-term fairness.

Builds the weekly remote-work plan for a team with more people than desks.
Hard constraints (desk capacity, mandatory office days, the company-wide cap
on remote days per week) are enforced strictly; personal limits (desired
days, minimum target, personal maximum) are treated as requests. Requests
are satisfied whenever possible; when they cannot all fit, persistent
fairness credits decide who concedes this week and grant that person
priority in the weeks to come.

Usage:
    python scheduler.py config.yaml [--dry-run] [--seed N] [--csv PATH]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from ortools.sat.python import cp_model

DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")

# Objective weights for the fairness phase. CP-SAT works on integer
# arithmetic, so the weights are scaled integers rather than fractions.
# Their relative magnitudes are what matters: one credit point must be worth
# less than a base preference (so credits reorder concessions rather than
# overturn them) and the penalty for an unrequested remote day must stay
# well below both, acting as a gentle preference for volunteers.
PREFERENCE_WEIGHT = 100   # value of one satisfied request unit
CREDIT_WEIGHT = 50        # extra weight per fairness credit point
UNREQUESTED_PENALTY = 30  # cost of assigning an unrequested remote day
TIE_BREAK_NOISE = 5       # upper bound of the random tie-breaking noise

MEMBER_KEYS = {"name", "desired_smart_days", "mandatory_office_days",
               "min_smart_days", "max_smart_days"}


@dataclass(frozen=True)
class Member:
    """A team member with their requests and hard commitments."""

    name: str
    desired: frozenset[str]   # days they would like to work remotely
    office: frozenset[str]    # days they must spend in the office (hard)
    min_smart: int            # remote-days target (soft)
    max_smart: int            # remote-days cap (soft)

    @property
    def free_days(self) -> int:
        """Days not blocked by mandatory office presence."""
        return len(DAYS) - len(self.office)


Plan = dict[str, list[str]]


def load_config(path: str) -> tuple[int, int, list[Member], str]:
    """Parse and validate the YAML configuration.

    Returns desks, the hard company-wide cap on weekly remote days, the
    member list, and the state-file path. All validation errors are
    collected and reported together, so a broken file can be fixed in a
    single pass.
    """
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    errors: list[str] = []
    for key in ("max_smart_per_week", "desks"):
        if key not in cfg:
            errors.append(f"missing '{key}'")
    if not cfg.get("members"):
        errors.append("missing the 'members' list")

    # max_smart_per_week is company policy and applies to everyone as a
    # hard ceiling; per-person limits below it fall back to it when omitted.
    hard_cap = cfg.get("max_smart_per_week", 0)
    default_min = cfg.get("min_smart_per_week", 0)

    members: list[Member] = []
    for raw in cfg.get("members", []):
        name = raw.get("name")
        if not name:
            errors.append("a member is missing the 'name' field")
            continue
        # Unknown keys are rejected rather than ignored: a typo in an
        # optional key would otherwise silently drop someone's constraint.
        if unknown := set(raw) - MEMBER_KEYS:
            errors.append(f"{name}: unknown key(s) {sorted(unknown)} "
                          f"(valid keys: {sorted(MEMBER_KEYS)})")
        # Day names are case-insensitive in the configuration.
        desired = [d.lower() for d in raw.get("desired_smart_days") or []]
        office = [d.lower() for d in raw.get("mandatory_office_days") or []]
        for day in (*desired, *office):
            if day not in DAYS:
                errors.append(f"{name}: invalid day '{day}' "
                              f"(valid days: {', '.join(DAYS)})")
        if overlap := set(desired) & set(office):
            errors.append(f"{name}: {sorted(overlap)} listed both as desired "
                          "smart day and mandatory office day")
        min_smart = raw.get("min_smart_days", default_min)
        max_smart = raw.get("max_smart_days", hard_cap)
        if max_smart > hard_cap:
            errors.append(f"{name}: max_smart_days ({max_smart}) exceeds the "
                          f"company-wide max_smart_per_week ({hard_cap})")
        if min_smart > hard_cap:
            errors.append(f"{name}: min_smart_days ({min_smart}) exceeds the "
                          f"company-wide max_smart_per_week ({hard_cap})")
        if min_smart > max_smart:
            errors.append(f"{name}: min_smart_days ({min_smart}) exceeds "
                          f"max_smart_days ({max_smart})")
        members.append(Member(name, frozenset(desired), frozenset(office),
                              min_smart, max_smart))

    if errors:
        print("Configuration errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    return cfg["desks"], hard_cap, members, cfg.get("state_file", "state.json")


def load_state(path: str) -> dict:
    """Load the persistent fairness state, starting fresh if absent."""
    file = Path(path)
    if file.exists():
        return json.loads(file.read_text(encoding="utf-8"))
    return {"credits": {}}


def save_state(path: str, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def check_feasibility(desks: int, hard_cap: int,
                      members: list[Member]) -> None:
    """Abort when no valid plan can exist; warn about inevitable concessions.

    Soft limits never make the problem infeasible, so most findings are
    advisory notes printed before solving. The blocking conditions are the
    hard ones: a day on which mandatory office presence leaves fewer people
    available for remote work than the desk shortage requires, or a weekly
    desk shortage that the company-wide cap cannot absorb.
    """
    required_remote = max(0, len(members) - desks)

    if desks >= len(members):
        print(f"Note: {desks} desks for {len(members)} people; capacity is "
              "never binding and the scheduler only distributes preferences.")

    # A target larger than the person's free days can never be fully met,
    # so it would accrue credit indefinitely. Worth flagging every run.
    for m in members:
        if m.min_smart > m.free_days:
            print(f"Note: {m.name} targets {m.min_smart} smart days but only "
                  f"{m.free_days} are free of mandatory office presence; the "
                  "gap will accrue fairness credit every week.")

    # When the personal maximums cannot absorb the weekly desk shortage but
    # the company cap still can, the difference will be assigned as forced
    # remote days. Announcing the exact amount upfront keeps the plan from
    # surprising anyone.
    personal_capacity = sum(min(m.max_smart, m.free_days) for m in members)
    hard_capacity = sum(min(hard_cap, m.free_days) for m in members)
    needed = len(DAYS) * required_remote
    if personal_capacity < needed <= hard_capacity:
        print(f"Note: covering the desk shortage takes {needed} smart days "
              f"per week but the personal maximums provide "
              f"{personal_capacity}; {needed - personal_capacity} forced "
              "smart day(s) will be assigned and compensated with fairness "
              "credits.")

    blocking = []
    # Unlike the personal maximums, the company-wide cap is hard: when even
    # everyone at the cap cannot cover the shortage, no plan exists.
    if needed > hard_capacity:
        blocking.append(
            f"covering the desk shortage takes {needed} smart days per week, "
            f"but with everyone at the company-wide cap of {hard_cap} (and "
            f"their mandatory office days) at most {hard_capacity} are "
            "available; raise max_smart_per_week or add desks")
    for day in DAYS:
        eligible = sum(1 for m in members if day not in m.office)
        if eligible < required_remote:
            present = ", ".join(m.name for m in members if day in m.office)
            blocking.append(
                f"{day}: {required_remote} people must work remotely, but "
                f"mandatory office presence ({present}) leaves only "
                f"{eligible} available")
    if blocking:
        print("No valid plan exists:", file=sys.stderr)
        for issue in blocking:
            print(f"  - {issue}", file=sys.stderr)
        sys.exit(1)


def solve(desks: int, hard_cap: int, members: list[Member],
          credits: dict[str, int], rng: random.Random) -> tuple[Plan, int]:
    """Compute the weekly plan with a two-phase lexicographic optimization.

    Phase 1 maximizes the number of accommodated request units (desired days
    granted, minimum targets reached, forced days avoided): a request is
    never sacrificed if it could have been satisfied. Phase 2 re-optimizes
    within the phase-1 optimum, letting fairness credits choose who concedes
    when not every request fits.

    Returns the plan and the total number of desired days requested.
    """
    model = cp_model.CpModel()

    # remote[i, di] is true when member i works remotely on day di.
    remote = {(i, di): model.NewBoolVar(f"remote_{m.name}_{day}")
              for i, m in enumerate(members)
              for di, day in enumerate(DAYS)}

    # Personal limits are modeled as elastic bounds: instead of constraining
    # the weekly total directly, slack variables measure how far the plan
    # deviates, and the objective drives that slack to zero. The slack stays
    # available to the credit accounting when a deviation is unavoidable.
    shortfall: dict[int, cp_model.IntVar] = {}  # days short of the target
    overflow: dict[int, cp_model.IntVar] = {}   # days forced above the cap
    for i, m in enumerate(members):
        total = sum(remote[i, di] for di in range(len(DAYS)))
        # The company-wide cap and the mandatory office days are the hard
        # personal constraints; everything else is elastic.
        model.Add(total <= hard_cap)
        for di, day in enumerate(DAYS):
            if day in m.office:
                model.Add(remote[i, di] == 0)
        # A personal maximum below the company cap is a preference: it can
        # be exceeded under desk pressure, but never beyond the cap itself.
        ceiling = min(hard_cap, m.free_days)
        if m.max_smart < ceiling:
            overflow[i] = model.NewIntVar(0, ceiling - m.max_smart,
                                          f"over_{m.name}")
            model.Add(overflow[i] >= total - m.max_smart)
        if m.min_smart > 0:
            shortfall[i] = model.NewIntVar(0, m.min_smart, f"short_{m.name}")
            model.Add(shortfall[i] >= m.min_smart - total)

    # Desk capacity, the other hard constraint: keeping the office at or
    # below `desks` people is equivalent to requiring at least
    # len(members) - desks people remote on every day.
    for di in range(len(DAYS)):
        model.Add(sum(remote[i, di] for i in range(len(members)))
                  >= len(members) - desks)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10

    def optimize() -> None:
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # The upfront checks cover the common impossibilities; rare
            # interactions between mandatory office days and the weekly cap
            # can still slip through and surface here.
            print("No valid plan exists: the combination of mandatory office "
                  "days, the company-wide cap and the desk shortage cannot "
                  "be satisfied; raise max_smart_per_week or add desks.",
                  file=sys.stderr)
            sys.exit(1)

    # Phase 1: every desired day granted counts +1, every concession -1.
    # Maximizing this balance finds how much accommodation is possible at
    # all, before fairness is allowed to express any preference.
    desired_vars = [remote[i, di] for i, m in enumerate(members)
                    for di, day in enumerate(DAYS) if day in m.desired]
    concessions = [*shortfall.values(), *overflow.values()]
    satisfaction = sum(desired_vars) - sum(concessions)

    if desired_vars or concessions:
        model.Maximize(satisfaction)
        optimize()
        # Pin the optimum: phase 2 may only choose among the plans that
        # accommodate this many request units, never fewer.
        model.Add(satisfaction == round(solver.ObjectiveValue()))

    # Phase 2: re-weight the same quantities by each member's credit, so
    # that when someone must concede, the burden lands on whoever conceded
    # least in the past. The small random noise breaks ties between
    # otherwise identical plans, preventing systematic favoritism (such as
    # configuration order deciding every draw).
    terms = []
    for i, m in enumerate(members):
        weight = PREFERENCE_WEIGHT + CREDIT_WEIGHT * credits.get(m.name, 0)
        for di, day in enumerate(DAYS):
            noise = rng.randint(0, TIE_BREAK_NOISE)
            if day in m.desired:
                terms.append((weight + noise) * remote[i, di])
            else:
                # Mildly discourage unrequested remote days so that daily
                # quotas are filled by volunteers first.
                terms.append(-(UNREQUESTED_PENALTY + noise) * remote[i, di])
        # Concessions inherit the member's credit weight: shorting or
        # forcing a high-credit person costs the objective more.
        for concession in (shortfall.get(i), overflow.get(i)):
            if concession is not None:
                noise = rng.randint(0, TIE_BREAK_NOISE)
                terms.append(-(weight + noise) * concession)
    model.Maximize(sum(terms))
    optimize()

    plan = {m.name: [day for di, day in enumerate(DAYS)
                     if solver.Value(remote[i, di])]
            for i, m in enumerate(members)}
    return plan, len(desired_vars)


def update_credits(members: list[Member], plan: Plan,
                   credits: dict[str, int]) -> list[tuple]:
    """Translate this week's outcome into fairness credits.

    Each member accrues one credit per unmet request unit. Refused desired
    days and the shortfall toward the minimum target overlap (a refused day
    usually is the day missing from the target), so taking their maximum
    counts each grievance once; days forced above the cap are a separate
    grievance and add on top. Members whose requests were all met have
    their credit reset.
    """
    report = []
    for m in members:
        granted = set(plan[m.name])
        refused = sorted(m.desired - granted)
        short = max(0, m.min_smart - len(granted))
        forced = max(0, len(granted) - m.max_smart)
        unmet = max(len(refused), short) + forced
        if unmet:
            credits[m.name] = credits.get(m.name, 0) + unmet
        elif m.desired or m.min_smart > 0 or m.max_smart < len(DAYS):
            # Reset only members who actually expressed a request: others
            # have nothing to be repaid for.
            credits[m.name] = 0
        report.append((m.name, refused, short, forced,
                       credits.get(m.name, 0)))
    return report


def print_plan(members: list[Member], plan: Plan, desks: int) -> None:
    """Print the person-by-day table with daily desk occupancy."""
    width = max(len(m.name) for m in members) + 2
    header = "".ljust(width) + "".join(day.center(7) for day in DAYS)
    print("\n" + header)
    print("-" * len(header))
    for m in members:
        cells = ("SMART" if day in plan[m.name]
                 else "OFF*" if day in m.office
                 else "off"
                 for day in DAYS)
        print(m.name.ljust(width) + "".join(c.center(7) for c in cells))
    print("-" * len(header))
    occupancy = ("{}/{}".format(
        sum(1 for m in members if day not in plan[m.name]), desks)
        for day in DAYS)
    print("in office".ljust(width) + "".join(o.center(7) for o in occupancy))
    print("\n(OFF* = mandatory office presence, off = in office, "
          "SMART = remote work)")


def export_csv(members: list[Member], plan: Plan, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["name", *DAYS])
        for m in members:
            writer.writerow([m.name, *("smart" if day in plan[m.name]
                                       else "office" for day in DAYS)])
    print(f"\nPlan exported to: {path}")


def print_fairness(report: list[tuple]) -> None:
    """Print each member's grievances and updated credit balance."""
    print("\nFairness:")
    for name, refused, short, forced, credit in report:
        grievances = []
        if refused:
            grievances.append(f"NOT accommodated on {', '.join(refused)}")
        if short:
            grievances.append(f"{short} smart day(s) below the target")
        if forced:
            grievances.append(f"FORCED {forced} smart day(s) above their maximum")
        if grievances:
            print(f"  - {name}: {'; '.join(grievances)} -> credit {credit} "
                  "(will get priority next week)")
        else:
            print(f"  - {name}: all requests satisfied (credit {credit})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly smart-working scheduler with long-term fairness")
    parser.add_argument("config", help="YAML configuration file")
    parser.add_argument("--dry-run", action="store_true",
                        help="do not update the fairness credits")
    parser.add_argument("--seed", type=int, default=None,
                        help="random seed (default: current ISO week)")
    parser.add_argument("--csv", default=None,
                        help="output CSV path (default: plan_<year>-w<week>.csv)")
    args = parser.parse_args()

    desks, hard_cap, members, state_file = load_config(args.config)
    check_feasibility(desks, hard_cap, members)

    # Seeding with the ISO week makes reruns within the same week
    # reproducible while still varying the tie-breaks across weeks.
    year, week, _ = date.today().isocalendar()
    rng = random.Random(args.seed if args.seed is not None
                        else year * 100 + week)

    state = load_state(state_file)
    credits = state.setdefault("credits", {})

    plan, total_desired = solve(desks, hard_cap, members, credits, rng)
    print_plan(members, plan, desks)

    satisfied = sum(1 for m in members
                    for day in plan[m.name] if day in m.desired)
    if total_desired:
        if satisfied == total_desired:
            print(f"\nAll {total_desired} requested smart days were accommodated.")
        else:
            print(f"\nOnly {satisfied} of {total_desired} requested smart days "
                  "could be accommodated this week: fairness credits decided "
                  "who is left out.")

    print_fairness(update_credits(members, plan, credits))
    export_csv(members, plan, args.csv or f"plan_{year}-w{week:02d}.csv")

    if args.dry_run:
        print("\n[dry-run] Credits NOT saved.")
    else:
        save_state(state_file, state)
        print(f"Credits updated in: {state_file}")


if __name__ == "__main__":
    main()
