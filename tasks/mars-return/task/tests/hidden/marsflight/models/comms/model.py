"""The communications model the core calls. Not wired in yet: the hooks below say where.

Design fields (per element, all optional; an element without them keeps MSL's rover X-band chain, a 15 W SSPA
and low-gain antennas, which the 60 kg ``AVIONICS_PER_ELEMENT_KG`` and the 300 W ``BASE_POWER_W`` already
carry):

    "comms": {"band": "X" | "Ka", "hga_m": 2.0, "tx_rf_W": 100, "gimbal": true, "uhf": true}

and one mission field, ``"dsn": {"passes_per_day": 3}`` (default 3: every complex's view is booked).

What the model does:
- ``mass_items``: the telecom mass above the default chain (HGA, gimbal, larger amplifier, UHF radio).
- ``power_delta_W``: the transmit power above the default chain's 69.4 W (SDST 14.4 W plus SSPA 55 W), to add
  to ``BASE_POWER_W`` for that element.
- ``obs``: what the flight software may know about its link (contact, Sun-Earth-probe angle, light time, the
  downlink rate the link closes at). Nothing hidden: all of it follows from public ephemerides and the design.
- ``check_ignition``: a cruise burn (heliocentric arc) is a ground-planned correction; it needs an uplink in the
  previous ``UPLINK_MAX_AGE_S`` and none during the conjunction moratorium (Sun-Earth-probe angle below 2 deg).
  A refused burn is a command error with the reason.
- ``edl_sample`` and ``edl_report``: during Mars entry the core hands over samples; at touchdown (or failure) the
  report says how much of EDL reached the ground, and ``entry_data_lost`` if nothing did.
"""
from __future__ import annotations

import math

import numpy as np

from ...core.model import MassItem, Model
from . import dsn, edl
from .link import downlink, element_radio, table, telecom_sizing

UPLINK_MAX_AGE_S = 7 * 86400.0
DEFAULT_DC_W = 14.4 + 55.0


def element_comms(design, eid):
    for e in design.get("elements", []) or []:
        if e.get("id") == eid:
            return dict(e.get("comms") or {})
    return {}


class CommsModel(Model):
    name = "comms_dsn"
    area = "comms"
    order = 70

    def configure(self, ctx):
        self.ctx = ctx
        d = ctx.design or {}
        self.passes = int((d.get("dsn") or {}).get("passes_per_day", 3))
        self.contact = dsn.Contact(self.passes)
        self.radios = {e.get("id"): element_comms(d, e.get("id")) for e in d.get("elements", []) or []}
        self.edl = []
        self.last_uplink_t = None

    def design_fields(self):
        return ["elements[].comms.band", "elements[].comms.hga_m", "elements[].comms.tx_rf_W",
                "elements[].comms.gimbal", "elements[].comms.uhf", "dsn.passes_per_day"]

    # ------------------------------------------------------------ mass and power
    def mass_items(self, vehicle):
        base = telecom_sizing({})["mass_kg"]
        out = []
        for e in vehicle.elements:
            c = self.radios.get(e.id) or {}
            if not c:
                continue
            m = telecom_sizing(c)["mass_kg"] - base
            if m > 0:
                out.append(MassItem(e.id, "telecom", m))
        return out

    def power_delta_W(self, eid, transmitting=True):
        c = self.radios.get(eid) or {}
        if not c:
            return 0.0
        s = telecom_sizing(c)
        return (s["dc_W"] if transmitting else s["standby_W"]) + s["dc_uhf_W"] - DEFAULT_DC_W

    # ------------------------------------------------------------ contact
    def best_radio(self, ids):
        """The element in the stack with the strongest downlink EIRP."""
        best = None
        for eid in ids:
            r = element_radio(self.radios.get(eid) or {})
            eirp = 10 * math.log10(r["tx_rf_W"]) + r["gain_dBi"]
            if best is None or eirp > best[0]:
                best = (eirp, eid, r)
        return best[1], best[2]

    def link_state(self, r, central, t, ids):
        st = self.contact.state(r, central, t)
        if st["uplink"]:
            self.last_uplink_t = t if self.last_uplink_t is None else max(self.last_uplink_t, t)
        out = {"dsn_contact": st["uplink"], "downlink": st["downlink"], "sep_deg": round(st["sep_deg"], 3),
               "owlt_s": round(st["owlt_s"], 3), "moratorium": st["moratorium"]}
        if st["stations_down"] and ids:
            eid, radio = self.best_radio(ids)
            c = st["stations_down"][0]
            ln = downlink(radio, st["range_m"], "34m", c, st["elevation_deg"][c], f_Hz=None)
            out["downlink_bps"] = round(ln.max_rate_bps(), 1) if st["downlink"] else 0.0
        return out

    def obs(self, state):
        r, central, t, ids = state_view(state)
        if r is None:
            return {}
        return {"comms": self.link_state(r, central, t, ids)}

    def had_uplink(self, r, central, t, lookback=UPLINK_MAX_AGE_S, step=3600.0):
        """Whether any uplink window fell in the ``lookback`` before ``t`` (the vehicle's position is held,
        which moves the view geometry by well under a degree over a week in cruise)."""
        if self.last_uplink_t is not None and t - self.last_uplink_t <= lookback:
            return True
        k = 0.0
        while k <= lookback:
            if self.contact.state(r, central, t - k)["uplink"]:
                self.last_uplink_t = t - k
                return True
            k += step
        return False

    def check_ignition(self, state, engine_id):
        """None if the ignition may go ahead, else the reason it is refused."""
        r, central, t, _ = state_view(state)
        if r is None or central != "Sun":
            return None
        st = self.contact.state(r, central, t)
        if st["moratorium"]:
            return (f"{engine_id}: correction burn not uplinked: solar conjunction command moratorium "
                    f"(Sun-Earth-probe angle {st['sep_deg']:.2f} deg, below 2 deg)")
        if not self.had_uplink(r, central, t):
            return f"{engine_id}: correction burn not uplinked: no DSN uplink in the previous 7 days"
        return None

    # ------------------------------------------------------------ EDL
    def edl_sample(self, t, r_mars, airspeed, alt):
        self.edl.append((float(t), np.asarray(r_mars, float), float(airspeed), float(alt)))

    def edl_report(self, orbiters=()):
        if not self.edl:
            return None
        rep = edl.coverage(self.edl, orbiters)
        ev = {"kind": "edl_data", "dte_fraction": round(rep["dte_fraction"], 3),
              "relay_fraction": round(rep["relay_fraction"], 3), "covered_fraction": round(rep["covered_fraction"], 3),
              "uhf_blackout_s": round(rep["uhf_blackout_s"], 1)}
        if rep["lost"]:
            ev["kind"] = "entry_data_lost"
            ev["reason"] = "no direct-to-Earth link and no relay orbiter in view during entry, descent and landing"
        return ev


def state_view(state):
    """(r from central, central, t, present element ids) from a Mission-like object or a dict."""
    if isinstance(state, dict):
        return state.get("r"), state.get("central"), state.get("t"), state.get("ids", [])
    x = getattr(state, "x", None)
    ids = [s.el.id for s in state.present()] if hasattr(state, "present") else []
    if x is None:
        if getattr(state, "central", None) == "Earth":
            return None, None, None, ids    # on the pad: ground lines, not the DSN
        r = state.site_state()[:3] if hasattr(state, "site_state") else None
        return r, state.central, state.t, ids
    return np.asarray(x[:3]), state.central, state.t, ids


MODELS = [CommsModel]
