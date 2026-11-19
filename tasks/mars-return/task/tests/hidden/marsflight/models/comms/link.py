"""Radio link budgets in the design control table (DCT) form JPL uses.

No new physics here: each line is a line of a DSN design control table, with the station values from the DSN
Telecommunications Link Design Handbook 810-005 (``data/comms/dsn.json``) and the spacecraft values from the
DESCANSO design and performance summaries (``data/comms/hardware.json``). The sums are:

    EIRP      = P_tx + circuit loss + G_sc + pointing loss                          (dBm)
    space     = -20 log10(4 pi R f / c)                                              (dB)
    P_r       = EIRP + space + atmosphere + G_station - station losses - polarization (dBm)
    T_sys     = T_AMW + T_M (1 - 1/L_atm) + 2.725 / L_atm                            (K, 810-005 105 eqs. 1, 2)
    Pt/N0     = P_r - 10 log10(k T_sys) - 30                                         (dB-Hz)
    Pd/N0     = Pt/N0 + 10 log10(sin^2 beta)    (residual carrier, modulation index beta; suppressed: 0 dB)
    margin    = Pd/N0 - (Eb/N0 threshold + 10 log10(rate) + implementation loss)

``tests`` in dev/test_comms_range.py rebuild three published DCTs from their inputs (MarCO X-band and UHF,
DESCANSO 18, Tables 5-4 and 5-5; Mars Odyssey's initial acquisition, DESCANSO 2, Table 5-3) within 1 dB.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

C = 299792458.0
K_DBM = 10.0 * math.log10(1.380649e-23) + 30.0        # Boltzmann's constant in dBm/K/Hz: -198.60
T_CMB = 2.725
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "comms")

_CACHE = {}


def table(name):
    """A JSON table from data/comms, read once."""
    if name not in _CACHE:
        with open(os.path.join(DATA, name + ".json")) as f:
            _CACHE[name] = json.load(f)
    return _CACHE[name]


def db(x):
    return 10.0 * math.log10(x)


def space_loss_dB(range_m, f_Hz):
    """Free-space loss, a negative number of dB."""
    return -20.0 * math.log10(4.0 * math.pi * range_m * f_Hz / C)


def dish_gain_dBi(diameter_m, f_Hz, efficiency=None):
    """Peak gain of a parabolic dish, eta (pi D f / c)^2. The efficiency default is fitted to MRO's 3 m HGA
    (46.7 dBi at 8.439 GHz, DESCANSO MRO Table 2-2), which gives 0.66."""
    eff = table("hardware")["hga"]["aperture_efficiency"] if efficiency is None else efficiency
    return db(eff * (math.pi * diameter_m * f_Hz / C) ** 2)


def atmosphere(band, complex_name, elevation_deg, cd=0.90):
    """Atmospheric loss (dB, positive) and its noise (K) at a DSN complex (810-005 105 eqs. 1 and 2)."""
    z = table("dsn")["atmosphere_zenith_dB"][band][complex_name]
    azen = z["0.90"] if cd >= 0.7 else z["0.50"]
    a = azen / math.sin(math.radians(max(elevation_deg, 1.0)))
    tm = 255.0 + 25.0 * cd
    lin = 10.0 ** (-a / 10.0)
    return a, tm * (1.0 - lin), T_CMB * lin


@dataclass
class Link:
    """One link, line by line. Losses are given as positive dB and subtracted."""
    f_Hz: float
    range_m: float
    tx_power_dBm: float
    tx_circuit_loss_dB: float
    tx_gain_dBi: float
    tx_pointing_loss_dB: float
    rx_gain_dBi: float
    rx_losses_dB: float          # receive pointing, wind, circuit (all positive)
    polarization_loss_dB: float
    atmosphere_dB: float
    t_sys_K: float
    lines: dict = field(default_factory=dict)

    @property
    def eirp_dBm(self):
        return self.tx_power_dBm - self.tx_circuit_loss_dB + self.tx_gain_dBi - self.tx_pointing_loss_dB

    @property
    def space_dB(self):
        return space_loss_dB(self.range_m, self.f_Hz)

    @property
    def received_dBm(self):
        return (self.eirp_dBm + self.space_dB - self.atmosphere_dB + self.rx_gain_dBi - self.rx_losses_dB
                - self.polarization_loss_dB)

    @property
    def n0_dBm_Hz(self):
        return K_DBM + db(self.t_sys_K)

    @property
    def pt_n0(self):
        return self.received_dBm - self.n0_dBm_Hz

    def pd_n0(self, mod_index_deg=None):
        """Data power to noise density. ``mod_index_deg`` None means a suppressed carrier (all power in data)."""
        if mod_index_deg is None:
            return self.pt_n0
        return self.pt_n0 + db(math.sin(math.radians(mod_index_deg)) ** 2)

    def margin_dB(self, rate_bps, code="turbo_1/6", mod_index_deg=None, impl_loss_dB=None):
        c = table("hardware")["codes"][code]
        il = c["implementation_loss_dB"] if impl_loss_dB is None else impl_loss_dB
        return self.pd_n0(mod_index_deg) - (c["eb_n0_dB"] + db(rate_bps) + il)

    def max_rate_bps(self, code="turbo_1/6", margin_dB=3.0, mod_index_deg=None):
        """The highest data rate that keeps ``margin_dB`` (3 dB is JPL's usual requirement)."""
        c = table("hardware")["codes"][code]
        x = self.pd_n0(mod_index_deg) - c["eb_n0_dB"] - c["implementation_loss_dB"] - margin_dB
        return 10.0 ** (x / 10.0)

    def report(self):
        return {"eirp_dBm": round(self.eirp_dBm, 2), "space_loss_dB": round(self.space_dB, 2),
                "received_dBm": round(self.received_dBm, 2), "t_sys_K": round(self.t_sys_K, 2),
                "n0_dBm_Hz": round(self.n0_dBm_Hz, 2), "pt_n0_dBHz": round(self.pt_n0, 2)}


def station(aperture="34m", band="X", direction="down"):
    return table("dsn")["antennas"][aperture][band] | {"aperture": aperture, "band": band, "direction": direction}


def downlink(sc, range_m, aperture="34m", complex_name="goldstone", elevation_deg=30.0, cd=0.90, f_Hz=None,
             sc_pointing_loss_dB=0.0, polarization_loss_dB=0.1):
    """Spacecraft to DSN. ``sc`` is a dict with tx_rf_W, circuit_loss_dB, gain_dBi and band ("X" or "Ka")."""
    band = sc.get("band", "X")
    st = table("dsn")["antennas"][aperture][band]
    f = f_Hz or table("hardware")["bands"][band]["down_Hz"]
    g = st["rx_gain_dBi"] + 20.0 * math.log10(f / (st["rx_f0_MHz"] * 1e6))   # 810-005 104 sec. 2.1.1.1
    a, t_atm, t_cmb = atmosphere(band, complex_name, elevation_deg, cd)
    return Link(f, range_m, db(sc["tx_rf_W"]) + 30.0, sc.get("circuit_loss_dB", 1.0), sc["gain_dBi"],
                sc_pointing_loss_dB, g, st["pointing_loss_dB"], polarization_loss_dB, a,
                st["tamw_K"] + t_atm + t_cmb)


def uplink(sc, range_m, aperture="34m", complex_name="goldstone", elevation_deg=30.0, cd=0.90,
           sc_pointing_loss_dB=0.0, sc_noise_K=None):
    """DSN to spacecraft at X band, 20 kW. ``sc`` holds rx_gain_dBi and rx_circuit_loss_dB."""
    st = table("dsn")["antennas"][aperture]["X"]
    hw = table("hardware")
    f = hw["bands"]["X"]["up_Hz"]
    a, _, _ = atmosphere("X", complex_name, elevation_deg, cd)
    t = sc_noise_K if sc_noise_K is not None else hw["transponder"]["noise_K"]
    return Link(f, range_m, db(st["tx_power_W"]) + 30.0, st["tx_waveguide_loss_dB"], st["tx_gain_dBi"],
                st["pointing_loss_dB"], sc.get("rx_gain_dBi", sc.get("gain_dBi", 0.0) - 1.0),
                sc.get("rx_circuit_loss_dB", 1.0) + sc_pointing_loss_dB, 0.3, a, t)


def uhf_relay(range_m, tx_power_W=None, tx_gain_dBi=None, rx_gain_dBi=None, f_Hz=None, tx_circuit_loss_dB=1.5,
              rx_circuit_loss_dB=1.1, polarization_loss_dB=2.73, t_sys_K=632.8):
    """A Mars surface or entry vehicle to an orbiter at UHF (Electra or Electra-Lite). Defaults are the MarCO
    receiving InSight case (DESCANSO 18, Table 5-5, 'Lander' column)."""
    u = table("hardware")["uhf"]
    return Link(f_Hz or u["return_Hz"], range_m, db(tx_power_W or u["tx_rf_W"]) + 30.0, tx_circuit_loss_dB,
                3.0 if tx_gain_dBi is None else tx_gain_dBi, 0.0, 3.5 if rx_gain_dBi is None else rx_gain_dBi,
                rx_circuit_loss_dB, polarization_loss_dB, 0.0, t_sys_K)


# ---------------------------------------------------------------- hardware sizing (mass and power)

def amplifier(tx_rf_W, band="X"):
    """DC power (W) and mass (kg) of the power amplifier for an RF output. Ka band: MRO's Ka TWTA. X band below
    20 W an SSPA (MSL rover,
    15 W RF from 55 W DC, 1.4 kg, DESCANSO 14 Tables 2-9 and 2-11), above it a TWTA (MSL descent stage 100 W RF
    from 175.2 W DC, 2.5 kg, Tables 2-11, 2-12; MRO 102 W from 172 W, DESCANSO MRO Table 2-1)."""
    hw = table("hardware")["amplifiers"]
    if band == "Ka":
        a = hw["twta_ka"]
    else:
        a = hw["sspa"] if tx_rf_W < hw["twta_min_rf_W"] else hw["twta"]
    dc = tx_rf_W / a["efficiency"]
    mass = a["mass_kg"] * max(1.0, tx_rf_W / a["rf_W"]) ** a["mass_exponent"]
    return dc, mass


def telecom_sizing(c):
    """Mass (kg) and DC power (W, transmitting) of one element's telecom from its design fields:
    ``c = {"band": "X"|"Ka", "hga_m": float (0 = no HGA), "tx_rf_W": float, "gimbal": bool, "uhf": bool}``."""
    hw = table("hardware")
    tx = float(c.get("tx_rf_W", hw["defaults"]["tx_rf_W"]))
    d = float(c.get("hga_m", 0.0) or 0.0)
    amp_dc, amp_kg = amplifier(tx, c.get("band", "X"))
    parts = {"transponder": hw["transponder"]["mass_kg"], "amplifier": amp_kg, "lga": hw["lga"]["mass_kg"],
             "microwave": hw["microwave_kg"]}
    if d > 0:
        parts["hga"] = hw["hga"]["areal_density_kg_m2"] * math.pi * d * d / 4.0 + hw["hga"]["feed_kg"]
        if c.get("gimbal", True):
            g = hw["hga"]["gimbal"]
            parts["hga_gimbal"] = max(g["min_kg"], g["kg_per_m2"] * d * d)
    if c.get("band") == "Ka":
        parts["ka_chain"] = hw["ka_chain_kg"]
    dc = hw["transponder"]["dc_W"] + amp_dc
    if c.get("uhf"):
        parts["uhf"] = hw["uhf"]["mass_kg"]
        dc_uhf = hw["uhf"]["dc_W"]
    else:
        dc_uhf = 0.0
    return {"mass_kg": sum(parts.values()), "parts_kg": parts, "dc_W": dc, "dc_uhf_W": dc_uhf,
            "standby_W": hw["transponder"]["rx_only_W"]}


def element_radio(c):
    """The link-budget view of an element's telecom design: transmit power, gain and band."""
    hw = table("hardware")
    band = c.get("band", "X")
    d = float(c.get("hga_m", 0.0) or 0.0)
    f = hw["bands"][band]["down_Hz"]
    if d > 0:
        gain, rx_gain = dish_gain_dBi(d, f), dish_gain_dBi(d, hw["bands"]["X"]["up_Hz"])
    else:
        gain, rx_gain = hw["lga"]["gain_dBi"], hw["lga"]["rx_gain_dBi"]
    return {"band": band, "tx_rf_W": float(c.get("tx_rf_W", hw["defaults"]["tx_rf_W"])), "gain_dBi": gain,
            "rx_gain_dBi": rx_gain, "circuit_loss_dB": hw["defaults"]["circuit_loss_dB"], "hga": d > 0}
