"""Fault protection for any flight software: a wrapper that answers the in-flight faults and the flown-failure modes.

(A verbatim copy of the simulator's models/failures/fault_protection.py, from the flight software runs in its
own process and cannot import the simulator. reference/guidance.py wraps the reference's Flight with it.)

    from fault_protection import FaultProtection
    fsw = FaultProtection(my_flight_software)      # an object with reset(info) and step(obs)

Each call it reads ``obs["faults"]`` and the elements' ``avionics`` status and adds the recovery action:

- an avionics string lost on an element in safe mode, with a healthy string left: ``switch_string:<id>``;
- an element rebooted by a radiation upset (``avionics.rebooted``): ``restart:<id>``;
- an RCS leak: ``isolate_rcs:<id>`` once.

It also guards against two flown losses that are flight software errors, when ``guards`` is on:
- Mars Polar Lander: the touchdown sensors are ignored until the vehicle is below ``touchdown_enable_m`` and has
  read touchdown on two calls in a row; the wrapped software sees ``touchdown_sensors`` only then.
- Schiaparelli: a negative height above the surface while still in flight (not landed) is not believed; the
  wrapped software sees the absolute value and ``height_suspect`` is set.

The file uses only the standard library, so the reference flight software (reference/gnc/) can carry a copy.
Proposed wiring there: ``reference/guidance.py`` builds ``_FL = FaultProtection(Flight(info))``.
"""
from __future__ import annotations


class FaultProtection:
    def __init__(self, inner, guards=True, touchdown_enable_m=40.0):
        self.inner, self.guards, self.enable_m = inner, guards, touchdown_enable_m
        self.isolated, self.td_count, self.log = set(), 0, []

    def reset(self, info):
        if hasattr(self.inner, "reset"):
            self.inner.reset(info)

    def step(self, obs):
        acts = []
        for el in obs.get("elements") or []:
            av = el.get("avionics") or {}
            if av.get("rebooted") and av.get("safe_mode"):
                acts.append("restart:" + el["id"])
            elif av.get("safe_mode") and av.get("strings", 1) == 2 and (av.get("healthy") or av.get("cross_strapped")):
                acts.append("switch_string:" + el["id"])
        for f in obs.get("faults") or []:
            if f.get("kind") == "rcs_leak" and f.get("element") not in self.isolated:
                self.isolated.add(f["element"])
                acts.append("isolate_rcs:" + f["element"])
            # a pressurant regulator stuck open (Peregrine) on a pressure fed stage: close the line on the call that
            # reports it, or the oxidizer tank ruptures (world 1005 lost 1,125 kg of MAV2's NTO in the cruise)
            if f.get("kind") == "tank_pressure_rising" and ("p", f.get("element")) not in self.isolated:
                self.isolated.add(("p", f["element"]))
                acts.append("isolate_pressurant:" + f["element"])
        seen = dict(obs)
        if self.guards:
            td = seen.pop("touchdown_sensors", None)
            h = seen.get("height_above_surface")
            if td is not None and any(td) and h is not None and abs(h) <= self.enable_m:
                self.td_count += 1
            else:
                self.td_count = 0
            if self.td_count >= 2:
                seen["touchdown_sensors"] = td
            if h is not None and h < 0 and seen.get("mode") == "flight":
                seen["height_above_surface"] = -h
                seen["height_suspect"] = True
        cmd = self.inner.step(seen)
        if acts:
            self.log.append((obs.get("met"), list(acts)))
            cmd = dict(cmd or {})
            cmd["actions"] = acts + list(cmd.get("actions") or [])
        return cmd

    def close(self):
        if hasattr(self.inner, "close"):
            self.inner.close()
