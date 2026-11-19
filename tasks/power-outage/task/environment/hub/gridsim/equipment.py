"""Local equipment, version 5: every area's own restoration problems.

Real control areas differ in more than their numbers. One restarts from a cascade of hydro plants on one river, one
has a city on an underground cable network that its protection lets back on only in small blocks, one stages a
nuclear plant's auxiliaries, one has an HVDC link to a neighbouring country. Each area here has two items of local
equipment of kinds no other area has, each with a control problem of its own (a sequence, a state to manage, an
interlock), described only in that area's private data (`area_data()["equipment"]`: prose, the public parameters and
the range of each parameter the control room does not know exactly). The first is the area's main problem (version
4's kinds); the second works on the same plant or district and changes how the first is handled (a dam whose control
was lost, hospitals transferring back into the city's pickup limits, steam turbines that must soak). The simulator
enforces both. A library written once for every area does not handle these, and an area mishandled loses most of its
restoration: the work grows with the number of areas and divides by area.

The exact values (`secret`) are hub-only: the item's text gives a range for each, as a control room's restoration plan
would ("the pond holds 8 to 24 MWh"), and drills on the true simulator show what happens. The study simulator plays
the same rules with the middle of each range, or with the values a team puts in the item's `assume` dict.

Kinds (one area each; which area has which stays on the hub). First items: hydro_cascade, urban_network,
nuclear_aux, lcc_hvdc, retrofit_crank, industrial_process, district_heating, undervoltage_relays, gas_linepack,
protection_reset, governor_mode, cable_centre, coal_auxiliaries, sync_condenser, load_restoration, house_load. Second
items: dam_crew, hospital_transfer, interruptible_contracts, converter_overvoltage, steam_soak, motor_voltage,
chp_steam, tap_changers, dual_fuel, rural_switching, ferranti_line, sympathetic_inrush, coal_mills, water_pumping,
reclosers, smart_loads.

Every item takes its commands as {"op": "equip", "id": <item id>, ...} (the item's text says which fields).

The assignment is fixed by the grid (the same at every team size and in every scenario) and stays on the hub with the
exact values; the study simulator gets the Rules class only and builds an area's equipment from the private data the
team passes it.
"""
import math
import random
from collections import deque

import numpy as np

CABLE_CHARGING = 10.0
CENTRE_SHARE = 0.55


def centre(grid, a, share=CENTRE_SHARE):
    """The area's load centre: the bus with the most load and the load buses nearest it, until they hold `share` of the
    area's load (nuclear station service left out). Returns bus indices, the hub first."""
    B = grid["buses"]
    bl = {}
    for ld in grid["loads"]:
        if ld["area"] == a and not ld.get("nuclear_aux"):
            bl[ld["bus"]] = bl.get(ld["bus"], 0.0) + ld["mw"]
    total = sum(bl.values())
    hub = max(bl, key=lambda i: (bl[i], -i))
    order = sorted(bl, key=lambda i: (math.dist((B[i]["x"], B[i]["y"]), (B[hub]["x"], B[hub]["y"])), i))
    out, acc = [], 0.0
    for i in order:
        out.append(i)
        acc += bl[i]
        if acc >= share * total:
            break
    return out


def value(it, k):
    """The parameter the simulator uses: the exact value on the hub, else the team's assumption, else the middle of
    the range the item's text gives."""
    for src in ("secret", "assume"):
        d = it.get(src)
        if isinstance(d, dict) and k in d:
            return float(d[k])
    lo, hi = it["ranges"][k]
    return (lo + hi) / 2.0


def _rng_text(it, k, unit, fmt="{:g}"):
    lo, hi = it["ranges"][k]
    return f"{fmt.format(lo)} to {fmt.format(hi)}{unit}"


# hub-only >>> (dev/build_study.py leaves this out: which area has what, and the exact values, are each area's
# private data and the hub's)
# Kinds (area, what it gates):
#
#   hydro_cascade      a01  the lower hydro plant runs on what the upper plant released travel_min earlier plus its head
#                           pond; it trips on low head when the pond runs dry
#   urban_network      a02  the city centre's feeders: picking up more than block_mw within window_min trips them all
#   nuclear_aux        a03  the nuclear plant's off-site power: its auxiliaries come back in stages, each needing reserve
#   lcc_hvdc           a04  an import from abroad: filter banks matched to the power, a strong AC side, a minimum power
#   retrofit_crank     a05  a gas turbine with a black-start diesel that can energise only a small dead network
#   industrial_process a06  an industrial zone: its feeders in the plant's order, each with enough capacity online
#   district_heating   a07  the city centre draws its electric heating unless the CHP unit has been at its must-run
#   undervoltage_relays a08 the city centre's feeders trip below a voltage threshold
#   gas_linepack       a09  every gas unit of the area burns the pipeline's linepack; its compressor refills it
#   protection_reset   a10  the city centre's substations need a crew to restore their protection settings
#   governor_mode      a11  the black-start hydro's governor: isolated mode to hold an island, interconnected once joined
#   cable_centre       a12  the city centre's lines are cables: each energised only from an island strong enough
#   coal_auxiliaries   a13  the coal units start only once their auxiliary boards have been fed long enough
#   sync_condenser     a14  the city centre can be energised beyond its first substation only with the condenser running
#   load_restoration   a15  automatic load restoration recloses the centre's feeders unless disarmed
#   house_load         a16  a coal unit tripped to house load: resynchronised before its boiler gives out, it is online
#                           at once
#
# Version 5 adds a second item to every area, a different real restoration problem that works on the first one's
# plant or zone, so the area's control logic changes, not just its numbers:
#
#   dam_crew           a01  the lower dam's control and level telemetry are lost: a crew at the plant, after taking
#                           over local control, is needed to run it or read the pond
#   hospital_transfer  a02  the centre's hospitals run on their own diesels and transfer back automatically, a block
#                           of load that counts in the network's pickup window, unless held (diesel fuel permitting)
#   interruptible_contracts a03 industrial customers: back only after notice, armed load counts as reserve for the
#                           nuclear pump starts, an interruption keeps them off for hours
#   converter_overvoltage a04 the converter station at the end of a long line: Ferranti rise on energisation, filter
#                           banks not absorbed by the import trip the station
#   steam_soak         a05  the steam units hold a soak load after synchronising (longer from warm and cold) or trip
#   motor_voltage      a06  the complex's motors start only with the bus voltage up, and stall if it sags after
#   chp_steam          a07  the CHP unit's steam section warms (hot / warm / cold) before it can pass its gas turbine's
#                           share, which the must-run may need
#   tap_changers       a08  the centre's on-load tap changers restore load as voltage sags unless blocked, for a while
#   dual_fuel          a09  the gas turbines can switch to oil from a small tank; the combined cycles trip on low
#                           pipeline pressure
#   rural_switching    a10  the rest of the area has no SCADA: switching there only by a crew that has taken local control
#   ferranti_line      a11  the city centre is reached over long lines: energising it needs reactive absorption and a
#                           load soon after
#   sympathetic_inrush a12  energising a centre transformer trips one energised shortly before, nearby
#   coal_mills         a13  the coal units' output above minimum comes from mills started one by one on the boards
#   water_pumping      a14  the pumping district's motors start only with voltage support, spaced in time
#   reclosers          a15  the area's feeder reclosers trip on cold load above their setting; a split feeder comes back
#                           in halves
#   smart_loads        a16  the outer feeders' chargers and heat pumps restart together a fixed time after supply returns
PLAN = (("hydro_cascade", "dam_crew"), ("urban_network", "hospital_transfer"),
        ("nuclear_aux", "interruptible_contracts"), ("lcc_hvdc", "converter_overvoltage"),
        ("retrofit_crank", "steam_soak"), ("industrial_process", "motor_voltage"), ("district_heating", "chp_steam"),
        ("undervoltage_relays", "tap_changers"), ("gas_linepack", "dual_fuel"), ("protection_reset", "rural_switching"),
        ("governor_mode", "ferranti_line"), ("cable_centre", "sympathetic_inrush"), ("coal_auxiliaries", "coal_mills"),
        ("sync_condenser", "water_pumping"), ("load_restoration", "reclosers"), ("house_load", "smart_loads"))

RANGES = {
    "hydro_cascade": dict(travel_min=(15, 45), pond_mwh=(8, 24), pond_start=(0.2, 0.6)),
    "urban_network": dict(block_mw=(40, 90), window_min=(8, 20)),
    "nuclear_aux": dict(reserve_mw=(40, 100), hold_min=(5, 15), handover_min=(20, 60), lockout_min=(20, 40)),
    "lcc_hvdc": dict(scr=(2.0, 3.0), bank_mw=(60, 90), min_mw=(30, 60)),
    "retrofit_crank": dict(prep_min=(20, 40), run_min=(10, 25), crank_mva=(25, 60)),
    "industrial_process": dict(ratio=(6, 12), gap_min=(3, 8), lockout_min=(15, 30)),
    "district_heating": dict(must_run=(0.45, 0.7), heat_min=(20, 50), trip_min=(4, 12)),
    "undervoltage_relays": dict(v_trip=(0.965, 0.99)),
    "gas_linepack": dict(pack_mwh=(400, 900), refill_mw=(250, 450), warmup_min=(10, 30)),
    "protection_reset": dict(work_min=(15, 40)),
    "governor_mode": dict(ratio=(2.0, 4.0), hunt_min=(5, 15)),
    "cable_centre": dict(strength_mw=(300, 600)),
    "coal_auxiliaries": dict(purge_min=(30, 60), aux_share=(0.05, 0.09)),
    "sync_condenser": dict(start_mw=(20, 40), runup_min=(8, 20)),
    "load_restoration": dict(delay_min=(2, 6), transformer_mw=(12, 25)),
    "house_load": dict(hold_min=(90, 240), block=(0.25, 0.4)),
    # version 5
    "dam_crew": dict(takeover_min=(10, 30)),
    "hospital_transfer": dict(retransfer_min=(5, 20), fuel_h=(3, 8)),
    "interruptible_contracts": dict(notice_min=(20, 60), restart_min=(60, 180), trip_hz=(59.4, 59.7)),
    "converter_overvoltage": dict(ferranti_mw=(150, 300), q_margin=(30, 80)),
    "steam_soak": dict(soak_min=(10, 30), soak_share=(0.12, 0.3)),
    "motor_voltage": dict(v_start=(0.97, 1.01), v_hold=(0.92, 0.95)),
    "chp_steam": dict(gt_share=(0.5, 0.65), warm_min=(15, 35)),
    "tap_changers": dict(recovery=(0.2, 0.45), block_max_min=(20, 60)),
    "dual_fuel": dict(oil_mwh=(80, 250), switch_min=(5, 15), trip_pct=(20, 40)),
    "rural_switching": dict(local_min=(5, 15)),
    "ferranti_line": dict(absorb_mvar=(60, 140), min_mw=(15, 45), delay_min=(3, 10)),
    "sympathetic_inrush": dict(settle_min=(5, 15)),
    "coal_mills": dict(warm_min=(10, 25), mill_mw=(3, 6)),
    "water_pumping": dict(v_start=(0.98, 1.02), stagger_min=(3, 8)),
    "reclosers": dict(setting=(1.4, 2.0)),
    "smart_loads": dict(resync_min=(5, 15), surge=(0.6, 1.2)),
}


def assign(grid):
    """The equipment of every area, from the grid itself, with its exact values. Mutates the grid where an item
    changes its data (a cable's charging). Returns {area: [item]}."""
    B, L, U, LD, SH = grid["buses"], grid["branches"], grid["units"], grid["loads"], grid["shunts"]
    cx = sum(b["x"] for b in B) / len(B)
    cy = sum(b["y"] for b in B) / len(B)
    out = {}
    for a, kinds in enumerate(PLAN):
        for j, kind in enumerate(kinds):
            # the first item's stream is version 4's, so its exact values stay what they were
            rng = random.Random(grid["seed"] * 7177 + 4000 + 1000 * j + a)
            name = f"a{a + 1:02d}"
            units = [u for u in U if u["area"] == a]
            ranges = {k: list(v) for k, v in RANGES[kind].items()}
            secret = {}
            for k, (lo, hi) in RANGES[kind].items():
                x = rng.uniform(lo, hi)
                secret[k] = round(x, 3) if hi - lo < 2 else round(x, 0)
            it = dict(id=f"{name}-{kind.replace('_', '-')}", kind=kind, ranges=ranges, secret=secret)
            cen = centre(grid, a)
            cen_ids = [B[i]["id"] for i in cen]
            cen_feeders = sorted(l["id"] for l in LD if l["bus"] in set(cen) and not l.get("nuclear_aux"))
            # the rest of the area's load buses (outside the centre): its outer district
            out_buses = sorted({l["bus"] for l in LD if l["area"] == a and l["bus"] not in set(cen)
                                and not l.get("nuclear_aux")})
            out_ids = [B[i]["id"] for i in out_buses]
            out_feeders = sorted(l["id"] for l in LD if l["bus"] in set(out_buses) and not l.get("nuclear_aux"))
            first = out[a][0] if j else None
            it_text = None
            if kind == "hydro_cascade":
                hy = sorted((u for u in units if u["kind"] == "hydro"), key=lambda u: u["id"])
                up, low = hy[0], hy[1]
                it.update(upper=up["id"], lower=low["id"], inflow_mw=round(0.05 * low["pmax"], 1), lockout_min=45.0,
                          valley=out_ids, valley_feeders=out_feeders)
                it_text = (
                    f"Hydro cascade: {up['id']} (upper) and {low['id']} (lower) are on one river. {low['id']} runs on its "
                    f"head pond, which is filled by what {up['id']} released {_rng_text(it, 'travel_min', ' minutes')} "
                    f"earlier (MW for MW) plus {it['inflow_mw']:g} MW of natural inflow; the pond holds "
                    f"{_rng_text(it, 'pond_mwh', ' MWh')} of {low['id']}'s generation and was "
                    f"{_rng_text(it, 'pond_start', '', '{:.0%}')} full at the blackout (above full it spills). If the pond "
                    f"runs dry {low['id']} trips on low head and cannot restart for 45 minutes. obs['equipment'] shows "
                    f"the pond level (pond_pct). The valley outside the city centre ({len(out_ids)} substations: "
                    f"{', '.join(out_ids)}) hangs on long lines whose voltage only {low['id']}'s regulator holds: a "
                    f"valley feeder can be picked up only while {low['id']} is online in its island, and every valley "
                    f"feeder trips when {low['id']} goes offline.")
            elif kind == "urban_network":
                it.update(buses=cen_ids, feeders=cen_feeders, lockout_min=20.0)
                it_text = (
                    f"Urban cable network: the city centre ({len(cen_ids)} substations: {', '.join(cen_ids)}) is fed by "
                    f"an underground cable network whose overcurrent protection is set tight. If the centre's feeders "
                    f"picked up within any {_rng_text(it, 'window_min', ' minutes')} draw more than "
                    f"{_rng_text(it, 'block_mw', ' MW')} in all (their draw at pickup, cold load included; feeders still "
                    f"closed come back when their bus is energised and count too), the protection trips every centre "
                    f"feeder picked up in that window and blocks pickup in the centre for 20 minutes.")
            elif kind == "nuclear_aux":
                nu = next(u for u in units if u["kind"] == "nuclear")
                it.update(plant=nu["id"], feeder=f"{nu['id']}-aux", stage_mw=[0.0, 25.0, 35.0])
                it_text = (
                    f"Nuclear auxiliaries: off-site power reaches {nu['id']}'s cooling only in stages. Stage 1 is its "
                    f"station-service feeder {nu['id']}-aux live and closed (30 MW). Stage 2 starts the cooling-water "
                    f"pumps (25 MW more) and stage 3 the reactor coolant pumps (35 MW more), each on "
                    f'{{"op": "equip", "id": "{it["id"]}", "stage": 2}} (then 3). A stage starts only after '
                    f"{_rng_text(it, 'hold_min', ' minutes')} at the one before, with the island within 0.1 Hz of 60 and "
                    f"at least the pumps' reserve in governor headroom online in the island ("
                    f"{_rng_text(it, 'reserve_mw', ' MW')} for stage 2, half as much again for stage 3); otherwise the "
                    f"pumps' start trips the station's boards back to nothing (the feeder opens) and they can be fed "
                    f"again only after {_rng_text(it, 'lockout_min', ' minutes')}. The plant hands its cooling over to "
                    f"off-site power after {_rng_text(it, 'handover_min', ' minutes')} at stage 3; only then does it "
                    f"count as restored (cooling_backup_until_min still applies; losing the supply before the handover "
                    f"starts the stages again). obs['equipment'] shows the stage and since when.")
            elif kind == "lcc_hvdc":
                mine = [i for i, b in enumerate(B) if b["area"] == a]
                bus = max(mine, key=lambda i: math.dist((B[i]["x"], B[i]["y"]), (cx, cy)))
                mw = float(rng.choice((300, 350)))
                it.update(bus=B[bus]["id"], mw_max=mw, ramp_mw_min=15.0, banks=4, bank_mvar=45.0, q_per_mw=0.55,
                          block_min=15.0, district=out_ids, district_feeders=out_feeders)
                it_text = (
                    f"LCC HVDC: a line-commutated link from the neighbouring country lands at bus {B[bus]['id']} and can "
                    f"import up to {mw:.0f} MW. Command it with "
                    f'{{"op": "equip", "id": "{it["id"]}", "mw": X}} (the import target; 0 blocks the link) and '
                    f'{{"op": "equip", "id": "{it["id"]}", "banks": N}} (AC filter banks switched in, 0 to 4, '
                    f"45 MVAr each, capacitive). The import follows its target at 15 MW/min, but a running converter "
                    f"cannot go below its minimum power ({_rng_text(it, 'min_mw', ' MW')}): deblocking starts the "
                    f"import at that minimum at once. The converter absorbs 0.55 MVAr per MW. It needs a strong AC "
                    f"side and its filters: if its island has less than {_rng_text(it, 'scr', '')} times the import in "
                    f"synchronous capacity online, or the import is more than the banks in service can filter "
                    f"({_rng_text(it, 'bank_mw', ' MW')} per bank), commutation fails, the import drops to 0 at once "
                    f"and the link blocks for 15 minutes; deblocking needs at least one bank in. A commutation failure "
                    f"is also a deep voltage dip at the converter's AC side: it trips every feeder of the district "
                    f"outside the city centre ({len(out_ids)} substations: {', '.join(out_ids)}) that is in the "
                    f"link's island (motor contactors and drives drop out). obs['equipment'] shows the import, its "
                    f"target, the banks and any block.")
            elif kind == "retrofit_crank":
                u = max((u for u in units if u["kind"] == "ct"), key=lambda u: (u["pmax"], u["id"]))
                it.update(unit=u["id"])
                it_text = (
                    f"Black-start retrofit: gas turbine {u['id']} ({u['pmax']:.0f} MW) has a small diesel that can crank "
                    f"it with its bus dead. The first start command with the bus dead starts the diesel, which is ready "
                    f"{_rng_text(it, 'prep_min', ' minutes')} later and then runs on its day tank for "
                    f"{_rng_text(it, 'run_min', ' minutes')}: a start of {u['id']} on the dead bus in that time goes "
                    f"ahead (its start time and failures as usual); after it the diesel stops and has to be started "
                    f"again. Cranked this way the turbine can energise only a small dead network: if the network "
                    f"connected to its bus when it comes online asks more than {_rng_text(it, 'crank_mva', ' MVA')} "
                    f"(line charging and transformer inrush, as for any energisation) it trips as it comes up. "
                    f"obs['equipment'] shows when the diesel is ready and until when it runs.")
            elif kind == "industrial_process":
                cands = sorted((l for l in LD if l["area"] == a and not l["priority"] and not l.get("nuclear_aux")),
                               key=lambda l: (-l["mw"], l["id"]))
                zone, acc = [], 0.0
                tot = sum(l["mw"] for l in LD if l["area"] == a)
                for l in cands:
                    zone.append(l)
                    acc += l["mw"]
                    if acc >= 0.45 * tot:
                        break
                rng.shuffle(zone)
                it.update(sequence=[l["id"] for l in zone])
                it_text = (
                    f"Industrial process: {len(zone)} feeders ({acc:.0f} MW at peak) serve an industrial complex whose "
                    f"motors and process lines restart in a fixed order: {', '.join(l['id'] for l in zone)}. Each must "
                    f"be picked up after every one before it is back and still on, at least "
                    f"{_rng_text(it, 'gap_min', ' minutes')} after the previous one, with the island within 0.1 Hz of 60 "
                    f"and at least {_rng_text(it, 'ratio', '')} times the feeder's demand in synchronous capacity online "
                    f"in the island. A pickup out of order or without that stalls the motors: the process interlock "
                    f"trips every feeder of the complex and they can be picked up again (from the first) only after "
                    f"{_rng_text(it, 'lockout_min', ' minutes')}. Feeders still closed count as picked up when their "
                    f"bus is energised.")
            elif kind == "district_heating":
                chp = max((u for u in units if u["kind"] == "ccgt"), key=lambda u: (u["pmax"], u["id"]))
                it.update(unit=chp["id"], buses=cen_ids, feeders=cen_feeders)
                it_text = (
                    f"District heating: combined-cycle unit {chp['id']} ({chp['pmax']:.0f} MW) is the CHP plant that heats "
                    f"the city centre ({len(cen_ids)} substations: {', '.join(cen_ids)}). Until it has run at its "
                    f"must-run output ({_rng_text(it, 'must_run', ' of its maximum', '{:.0%}')}) for "
                    f"{_rng_text(it, 'heat_min', ' minutes')}, the centre's buildings heat with electricity: a centre "
                    f"feeder that comes back overloads its distribution transformers and trips on overload "
                    f"{_rng_text(it, 'trip_min', ' minutes')} later (it can be closed again after 60 minutes, once "
                    f"its transformers have cooled). Once "
                    f"the heat is back its feeders hold; if the unit drops below its must-run for 10 minutes, the heat "
                    f"goes again and the centre's feeders trip as before.")
            elif kind == "undervoltage_relays":
                it.update(buses=cen_ids, feeders=cen_feeders, lockout_min=15.0)
                it_text = (
                    f"Undervoltage relays: the city centre's feeders ({len(cen_ids)} substations: {', '.join(cen_ids)}) "
                    f"have undervoltage load shedding armed: a centre feeder whose bus is below "
                    f"{_rng_text(it, 'v_trip', ' pu', '{:.3f}')} at a power flow trips and cannot be closed for 15 "
                    f"minutes.")
            elif kind == "gas_linepack":
                gas = sorted(u["id"] for u in units if u["kind"] in ("ct", "ccgt"))
                fs = sorted((l for l in LD if l["area"] == a and not l.get("nuclear_aux")), key=lambda l: l["id"])
                f = fs[rng.randrange(len(fs))]
                it.update(units=gas, feeder=f["id"], lockout_min=30.0)
                it_text = (
                    f"Gas linepack: the gas units {', '.join(gas)} burn the gas held in the area's pipeline, "
                    f"{_rng_text(it, 'pack_mwh', ' MWh')} of their output's worth at the blackout. Their output draws "
                    f"it down MW for MW. The compressor station on feeder {f['id']} refills it at "
                    f"{_rng_text(it, 'refill_mw', ' MW')}'s worth once the feeder has been supplied for "
                    f"{_rng_text(it, 'warmup_min', ' minutes')}, up to the level of the blackout. If the pipeline runs "
                    f"out every one of these units trips on low gas pressure and none can start again for 30 minutes. "
                    f"obs['equipment'] shows the pressure (pressure_pct, of the blackout's) and whether the compressor "
                    f"runs.")
            elif kind == "protection_reset":
                it.update(buses=cen_ids, feeders=cen_feeders)
                it_text = (
                    f"Protection settings: the relays of the city centre's {len(cen_ids)} substations "
                    f"({', '.join(cen_ids)}) went to their post-blackout group and cannot be set remotely. Until a crew "
                    f"has restored a substation's settings, any of its feeders trips the moment it picks up (and a "
                    f"closed one when its bus is energised). A crew at the substation does it with "
                    f'{{"op": "equip", "id": "{it["id"]}", "crew": C, "bus": B}}, which takes '
                    f"{_rng_text(it, 'work_min', ' minutes')} (the same at every substation) and keeps the crew there. "
                    f"obs['equipment'] shows the substations done.")
            elif kind == "governor_mode":
                hy = next(u for u in units if u["kind"] == "hydro")
                it.update(unit=hy["id"])
                it_text = (
                    f"Governor mode: black-start hydro {hy['id']}'s governor has two settings, "
                    f'{{"op": "equip", "id": "{it["id"]}", "mode": "isolated"}} or "interconnected" (now). In '
                    f"interconnected mode it answers frequency with a third of its governor reserve and three times its "
                    f"governor time constant; isolated mode gives the full response an island needs. In isolated mode "
                    f"in an island with more than {_rng_text(it, 'ratio', '')} times its maximum output in synchronous "
                    f"capacity online, it hunts against the other units' governors and trips on power swings after "
                    f"{_rng_text(it, 'hunt_min', ' minutes')}; the swings also trip every closed tie line of the "
                    f"area (out-of-step relays). obs['equipment'] shows the mode.")
            elif kind == "cable_centre":
                inside = [b for b in L if b["f"] in set(cen) and b["t"] in set(cen) and not b["tie"]]
                for b in inside:
                    b["b"] = round(b["b"] * CABLE_CHARGING, 6)
                    b["cable"] = True
                it.update(buses=cen_ids, cables=sorted(b["id"] for b in inside), lockout_min=30.0)
                it_text = (
                    f"Cable centre: the city centre's {len(inside)} lines between its substations "
                    f"({', '.join(sorted(b['id'] for b in inside))}) are underground cables with ten times the charging "
                    f"of an overhead line (your line data has it). The cable protection lets them be energised one at a "
                    f"time, each from an island with at least {_rng_text(it, 'strength_mw', ' MW')} of synchronous "
                    f"capacity online: energising a dead section that holds more than one of these cables (closing a "
                    f"cable counts it), or a cable from a weaker island, trips on overvoltage and locks those cables out "
                    f"for 30 minutes.")
            elif kind == "coal_auxiliaries":
                coal = sorted(u["id"] for u in units if u["kind"] == "coal")
                it.update(units=coal)
                it_text = (
                    f"Coal auxiliaries: coal units {', '.join(coal)} start only from their auxiliary boards. Feed a "
                    f"unit's board with {{\"op\": \"equip\", \"id\": \"{it['id']}\", \"unit\": U, \"board\": true}} "
                    f"(its bus must be live; the board draws {_rng_text(it, 'aux_share', ' of the unit maximum', '{:.0%}')} "
                    f"at the unit's bus from then on: mills, fans, pumps); the boiler purge and fill take "
                    f"{_rng_text(it, 'purge_min', ' minutes')} of continuous supply before a start is accepted. A board "
                    f"whose bus goes dark loses its purge, and a unit running without its board trips. A fed board "
                    f"also banks the boiler of a unit that is off (fans, feed pumps and auxiliary steam keep it "
                    f"warm): its metal stays as it was when the board was first fed, so a unit tripped at the "
                    f"blackout and fed soon after starts hot. obs['equipment'] shows each board and since when it "
                    f"is fed.")
            elif kind == "sync_condenser":
                hub = B[cen[0]]["id"]
                it.update(bus=hub, buses=cen_ids, q_mvar=100.0)
                it_text = (
                    f"Synchronous condenser: the city centre ({len(cen_ids)} substations: {', '.join(cen_ids)}) is fed "
                    f"over long lines whose charging only the synchronous condenser at {hub} can absorb. Until the "
                    f"condenser is running, energising a dead section that holds more than one centre substation trips "
                    f"on overvoltage: the centre comes back one substation at a time. Start it with "
                    f'{{"op": "equip", "id": "{it["id"]}", "on": true}} once {hub} is live: as a motor it draws '
                    f"{_rng_text(it, 'start_mw', ' MW')} from its island while it runs up (refused without that headroom) "
                    f"and runs after {_rng_text(it, 'runup_min', ' minutes')}; it stops if {hub} goes dark. obs['equipment'] "
                    f"shows its state.")
            elif kind == "load_restoration":
                it.update(buses=cen_ids)
                it_text = (
                    f"Automatic load restoration: the city centre's substations ({', '.join(cen_ids)}) have automatic "
                    f"load restoration armed. Some {_rng_text(it, 'delay_min', ' minutes')} after one of them is live it "
                    f"closes every feeder there that is open (including those under-frequency shedding opened), all at "
                    f"once. If what they draw then (cold load included) is more than the substation transformer's "
                    f"{_rng_text(it, 'transformer_mw', ' MW')}, its overcurrent protection trips them all and the "
                    f"substation's feeders cannot be closed for 60 minutes (after which the scheme tries again). Disarm "
                    f"the scheme at a substation with "
                    f'{{"op": "equip", "id": "{it["id"]}", "bus": B, "armed": false}} (needs remote control at the '
                    f"substation; armed true re-arms it). obs['equipment'] shows where it is armed.")
            elif kind == "house_load":
                u = max((u for u in units if u["kind"] == "coal"), key=lambda u: (u["pmax"], u["id"]))
                it.update(unit=u["id"])
                it_text = (
                    f"House load: coal unit {u['id']} ({u['pmax']:.0f} MW) tripped to house load at the blackout: it is "
                    f"still turning, feeding only its own auxiliaries. Its boiler can hold that for "
                    f"{_rng_text(it, 'hold_min', ' minutes')}, after which it trips like any other unit (hot). Until then "
                    f"a start command with its bus live resynchronises it within the step, at once at "
                    f"{_rng_text(it, 'block', ' of its maximum', '{:.0%}')} (the boiler cannot hold less once loaded), "
                    f"with its setpoint there; it then follows its target at its ramp rate. obs['equipment'] shows "
                    f"whether it is still on house load.")
            elif kind == "dam_crew":
                low = first["lower"]
                bus = B[next(u["bus"] for u in units if u["id"] == low)]["id"]
                it.update(unit=low, bus=bus)
                it_text = (
                    f"Dam control: the lower dam's control room lost its link in the blackout. {low}'s start, stop, "
                    f"setpoint, AGC and voltage commands are accepted only while one of your crews is at {bus} and has "
                    f"taken over local control there ({_rng_text(it, 'takeover_min', ' minutes')} after it arrives, "
                    f"again after every arrival), and the head pond's level (pond_pct) shows only then. The plant keeps "
                    f"its last setpoint, AGC and governor while nobody is there. obs['equipment'] shows whether a crew "
                    f"has local control.")
            elif kind == "hospital_transfer":
                pri = sorted(l["id"] for l in LD if l["bus"] in set(cen) and l["priority"] and l["area"] == a
                             and not l.get("nuclear_aux"))
                it.update(feeders=pri, buses=sorted({B[l["bus"]]["id"] for l in LD if l["id"] in set(pri)}))
                it_text = (
                    f"Hospital transfer: the centre's priority feeders ({', '.join(pri)}) supply hospitals and data "
                    f"centres that started their own diesels at the blackout. Once their feeder is back (closed and "
                    f"live) they draw nothing from it until their transfer switches put their whole load back on the "
                    f"grid at once, {_rng_text(it, 'retransfer_min', ' minutes')} later: that load (its cold load "
                    f"included) is then picked up and counts in the centre's pickup window like any pickup. A "
                    f"substation's transfers can be held with "
                    f'{{"op": "equip", "id": "{it["id"]}", "bus": B, "hold": true}} (false releases them; a released '
                    f"transfer goes {_rng_text(it, 'retransfer_min', ' minutes')} after the release), but the diesels "
                    f"carry {_rng_text(it, 'fuel_h', ' hours')} of fuel from the blackout: when it runs out every "
                    f"site still on diesel transfers at once if its feeder is back. A priority feeder counts as served "
                    f"once it is back. obs['equipment'] shows which feeders are still on diesel and which buses hold.")
            elif kind == "interruptible_contracts":
                cands = sorted((l for l in LD if l["area"] == a and l["bus"] in set(out_buses) and not l["priority"]
                                and not l.get("nuclear_aux")), key=lambda l: (-l["mw"], l["id"]))
                zone, acc = [], 0.0
                tot = sum(l["mw"] for l in LD if l["area"] == a and not l.get("nuclear_aux"))
                for l in cands:
                    zone.append(l["id"])
                    acc += l["mw"]
                    if acc >= 0.35 * tot:
                        break
                it.update(feeders=sorted(zone), plant=first["plant"], nuclear=first["id"])
                it_text = (
                    f"Interruptible contracts: {len(zone)} feeders ({acc:.0f} MW at peak: {', '.join(sorted(zone))}) "
                    f"supply industrial customers on interruptible contracts. A customer takes supply again only "
                    f'after the control room has called it, {{"op": "equip", "id": "{it["id"]}", "call": true}} (all '
                    f"of them), and {_rng_text(it, 'notice_min', ' minutes')} have passed for its plant to be ready; a "
                    f"feeder closed before that is refused. Once back, the load is armed: it trips at once if the "
                    f"island's frequency falls below {_rng_text(it, 'trip_hz', ' Hz', '{:.1f}')}, and it counts as "
                    f"reserve for {first['plant']}'s pump starts ({first['id']}): a stage started with less governor "
                    f"headroom than it needs but enough with the armed load trips that load instead of the station's "
                    f"boards. An interrupted customer (tripped by the scheme or a stage, under-frequency shedding, "
                    f"its bus going dark or opened by you) aborts its process and takes supply again only "
                    f"{_rng_text(it, 'restart_min', ' minutes')} later. obs['equipment'] shows whether they were called "
                    f"and until when each interrupted feeder stays off.")
            elif kind == "converter_overvoltage":
                it.update(bus=first["bus"], link=first["id"], lockout_min=15.0)
                it_text = (
                    f"Converter overvoltage: the converter station at {first['bus']} ({first['id']}) is at the end of a "
                    f"long line. Energising it from an island with less than {_rng_text(it, 'ferranti_mw', ' MW')} "
                    f"of synchronous capacity online raises its voltage past the station's overvoltage relay "
                    f"(Ferranti rise): the close trips. Once live, the filter banks in service must not give more than "
                    f"{_rng_text(it, 'q_margin', ' MVAr')} beyond what the converter absorbs (0.55 MVAr per MW of "
                    f"import): above that the relay trips every bank and blocks the link for 15 minutes (the import "
                    f"drops to 0). The check runs after the commands of each step, with the import the step ends at. "
                    f"obs['equipment'] shows the filters' MVAr not absorbed (unabsorbed_mvar).")
            elif kind == "steam_soak":
                st_units = sorted(u["id"] for u in units if u["kind"] in ("ccgt", "coal"))
                it.update(units=st_units, lockout_min=60.0)
                it_text = (
                    f"Steam soak: the steam turbines of {', '.join(st_units)} must soak after they synchronise: held "
                    f"at or below {_rng_text(it, 'soak_share', ' of their maximum', '{:.0%}')} for "
                    f"{_rng_text(it, 'soak_min', ' minutes')} after a hot start, twice that after a warm one and three "
                    f"times after a cold one (the start type the start command reports). Loaded above the soak share "
                    f"before that, the rotor's differential expansion trips the unit, which cannot start again for "
                    f"60 minutes, and then cold. A soaking unit does not trip for running below its minimum stable "
                    f"load. obs['equipment'] shows each unit's soak and until when.")
            elif kind == "motor_voltage":
                it.update(sequence=list(first["sequence"]), process=first["id"])
                it_text = (
                    f"Motor voltage: the industrial complex's motors ({first['id']}) start direct on line. A process "
                    f"feeder picks up only with its bus at {_rng_text(it, 'v_start', ' pu', '{:.2f}')} or more at the "
                    f"last power flow, and while the complex runs, any of its buses below "
                    f"{_rng_text(it, 'v_hold', ' pu', '{:.2f}')} at a power flow stalls its motors: both trip the "
                    f"process interlock as an out-of-order step does.")
            elif kind == "chp_steam":
                u = next(x for x in units if x["id"] == first["unit"])
                it.update(unit=u["id"])
                it_text = (
                    f"CHP steam section: {u['id']} ({u['pmax']:.0f} MW) can give no more than its gas turbine's share "
                    f"({_rng_text(it, 'gt_share', ' of its maximum', '{:.0%}')}) until its heat-recovery boiler and "
                    f"steam turbine have warmed: {_rng_text(it, 'warm_min', ' minutes')} after it comes online from "
                    f"a hot start, twice that from a warm one and three times from a cold one. Until then its maximum "
                    f"is that share (obs['equipment'] shows it).")
            elif kind == "tap_changers":
                it.update(buses=list(first["buses"]), feeders=list(first["feeders"]))
                it_text = (
                    f"Tap changers: the centre's substations ({', '.join(first['buses'])}) have on-load tap changers "
                    f"in automatic. When a substation's bus voltage sags below 1.0 pu they restore its customers' "
                    f"voltage and with it their load: its feeders draw up to {_rng_text(it, 'recovery', '', '{:.0%}')} "
                    f"more (in full at 0.97 pu). Blocked, with "
                    f'{{"op": "equip", "id": "{it["id"]}", "bus": B, "blocked": true}}, they hold their tap and the '
                    f"feeders draw with the voltage (squared) instead, but a block lasts at most "
                    f"{_rng_text(it, 'block_max_min', ' minutes')}: then the tap changers go back to automatic and a "
                    f"new block there is possible after as long again. obs['equipment'] shows where they are blocked "
                    f"and until when.")
            elif kind == "dual_fuel":
                cts = sorted(u["id"] for u in units if u["kind"] == "ct")
                cc = sorted(u["id"] for u in units if u["kind"] == "ccgt")
                it.update(units=cts, ccgt=cc, pipeline=first["id"])
                it_text = (
                    f"Dual fuel: the gas turbines {', '.join(cts)} can burn distillate oil from a shared tank holding "
                    f"{_rng_text(it, 'oil_mwh', ' MWh')} of their output. "
                    f'{{"op": "equip", "id": "{it["id"]}", "unit": U, "fuel": "oil"}} (or "gas") switches a unit: '
                    f"it takes {_rng_text(it, 'switch_min', ' minutes')}, during which an online unit gives at most "
                    f"half its maximum. On oil it does not draw on the pipeline ({first['id']}); when the tank is empty "
                    f"units on oil trip. The combined-cycle units {', '.join(cc)} have no oil: they trip on low gas "
                    f"pressure once the pipeline falls below {_rng_text(it, 'trip_pct', '%')} of its blackout level "
                    f"(and cannot start below it). obs['equipment'] shows each unit's fuel and the tank.")
            elif kind == "rural_switching":
                it.update(buses=out_ids, feeders=out_feeders)
                it_text = (
                    f"Rural switching: the substations outside the centre ({len(out_ids)}: {', '.join(out_ids)}) have "
                    f"indication-only RTUs: you see them, but their breakers and feeders can be switched only by one "
                    f"of your crews there that has taken local control, {_rng_text(it, 'local_min', ' minutes')} after "
                    f"it arrives (energising a line into one from a substation with remote control works as usual). "
                    f"obs['equipment'] shows where a crew has local control.")
            elif kind == "ferranti_line":
                it.update(buses=cen_ids, lockout_min=15.0)
                it_text = (
                    f"Ferranti rise: the city centre ({len(cen_ids)} substations: {', '.join(cen_ids)}) is reached over "
                    f"long lines. Energising a dead section that holds a centre substation trips on overvoltage, and "
                    f"locks every line into the centre out for 15 minutes, unless "
                    f"the island's online synchronous units can absorb at least "
                    f"{_rng_text(it, 'absorb_mvar', ' MVAr')} between them (the sum of their minimum reactive output, "
                    f"from your unit data); and when the centre comes back from dark it must carry at least "
                    f"{_rng_text(it, 'min_mw', ' MW')} {_rng_text(it, 'delay_min', ' minutes')} after its first "
                    f"substation is live (the area energises at most once every 5 minutes: feeders left closed on "
                    f"its dark substations come back with them), or its lines' overvoltage relays open every line "
                    f"into the centre and keep them open for 15 minutes. obs['equipment'] shows since when the "
                    f"centre is live.")
            elif kind == "sympathetic_inrush":
                tx = sorted(B[i]["id"] for i in cen)
                it.update(buses=tx)
                it_text = (
                    f"Sympathetic inrush: the centre's substation transformers ({', '.join(tx)}) sit on short cables "
                    f"to each other. Energising a section that holds one of them saturates any other centre transformer "
                    f"energised less than {_rng_text(it, 'settle_min', ' minutes')} before: that substation's feeders "
                    f"trip on the sympathetic inrush and cannot be closed for 15 minutes.")
            elif kind == "coal_mills":
                coal = sorted((u for u in units if u["kind"] == "coal"), key=lambda u: u["id"])
                it.update(units=[u["id"] for u in coal], mills={u["id"]: 5 for u in coal}, board=first["id"])
                it_text = (
                    f"Coal mills: {', '.join(u['id'] for u in coal)} run on oil support burners up to their minimum "
                    f"stable load; above it each of their 5 mills gives a fifth of the rest of their maximum. "
                    f'{{"op": "equip", "id": "{it["id"]}", "unit": U, "mills": N}} sets how many run (one more each '
                    f"step at most, on a running unit). A mill draws {_rng_text(it, 'mill_mw', ' MW')} from the unit's "
                    f"auxiliary board ({first['id']}) and gives its output "
                    f"{_rng_text(it, 'warm_min', ' minutes')} after it starts. A unit's maximum is its minimum stable "
                    f"load (today's) plus its warm mills' share. Mills stop when their unit stops or loses its board. "
                    f"obs['equipment'] shows each unit's mills.")
            elif kind == "water_pumping":
                it.update(buses=out_ids, feeders=out_feeders, condenser=first["id"], lockout_min=20.0)
                it_text = (
                    f"Water pumping: the district outside the centre ({len(out_ids)} substations: "
                    f"{', '.join(out_ids)}) feeds water and irrigation pumping stations with large motors. One of its "
                    f"feeders picks up only with its bus at {_rng_text(it, 'v_start', ' pu', '{:.2f}')} or more at "
                    f"the last power flow, or with the synchronous condenser ({first['id']}) running in its island, "
                    f"and at least {_rng_text(it, 'stagger_min', ' minutes')} after the district's last pickup (the "
                    f"pressure surge in the shared mains); otherwise the motors stall and trip it, and it cannot be "
                    f"closed for 20 minutes.")
            elif kind == "reclosers":
                fs = sorted(l["id"] for l in LD if l["area"] == a and not l.get("nuclear_aux"))
                it.update(feeders=fs, buses=out_ids, split_min=10.0)
                it_text = (
                    f"Reclosers: every feeder of the area has a recloser set at "
                    f"{_rng_text(it, 'setting', ' times the feeder peak', '{:.1f}')}. A feeder whose draw at pickup "
                    f"(cold load included) is above it trips at once, and after three such trips the recloser locks "
                    f"out until a crew resets it. A crew at the feeder's substation can split a feeder, "
                    f'{{"op": "equip", "id": "{it["id"]}", "crew": C, "feeder": F}} (10 minutes): a split feeder picks '
                    f"up half its load, and the other half 5 minutes later, as two pickups; it also resets a locked "
                    f"recloser. obs['equipment'] shows locked and split feeders.")
            elif kind == "smart_loads":
                it.update(buses=out_ids, feeders=out_feeders, decay_min=15.0, unit=first["unit"])
                it_text = (
                    f"Smart loads: the feeders outside the centre ({len(out_ids)} substations: {', '.join(out_ids)}) "
                    f"carry chargers and heat pumps that all restart {_rng_text(it, 'resync_min', ' minutes')} after "
                    f"their feeder's supply returns, by their grid code: each such feeder then draws "
                    f"{_rng_text(it, 'surge', ' of its demand', '{:.0%}')} more at once, decaying over 15 minutes.")
            it["text"] = it_text
            out.setdefault(a, []).append(it)
    return out
# hub-only <<<


class Rules:
    """The simulator side: state and checks, called from sim.Sim at the points each kind acts."""

    def __init__(self, sim, equipment):
        self.sim = sim
        s = sim
        self.items = {}
        self.by_area = {int(a): items for a, items in equipment.items()}
        self.area_of = {}
        for a, items in self.by_area.items():
            for it in items:
                self.items[it["id"]] = it
                self.area_of[it["id"]] = a
        self.st = {}                         # item id -> its state
        for iid, it in self.items.items():
            k = it["kind"]
            st = self.st.setdefault(iid, {})
            if k == "hydro_cascade":
                st.update(up=s.u_idx[it["upper"]], low=s.u_idx[it["lower"]], hist=deque(),
                          level=value(it, "pond_start") * value(it, "pond_mwh"))
            elif k in ("urban_network", "undervoltage_relays", "protection_reset", "district_heating"):
                st["f"] = {s.f_idx[f] for f in it["feeders"]}
                st["picks"] = []
                st["locked"] = -1.0
                st["done"] = {}
                st["since"] = None
                st["low_since"] = None
                if k == "protection_reset":
                    st["bus"] = {b: s.idx[b] for b in it["buses"]}
                if k == "district_heating":
                    st["u"] = s.u_idx[it["unit"]]
            elif k == "nuclear_aux":
                st.update(f=s.f_idx[it["feeder"]], stage=0, since=None, locked=-1.0, handed=False)
            elif k == "lcc_hvdc":
                st.update(b=s.idx[it["bus"]], target=0.0, p=0.0, blocked=-1.0, banks=0)
            elif k == "retrofit_crank":
                st.update(u=s.u_idx[it["unit"]], ready=None, until=None)
            elif k == "industrial_process":
                st.update(seq=[s.f_idx[f] for f in it["sequence"]], last=-1e9, locked=-1.0)
            elif k == "gas_linepack":
                st.update(units=[s.u_idx[u] for u in it["units"]], f=s.f_idx[it["feeder"]],
                          pack=value(it, "pack_mwh"), full=value(it, "pack_mwh"), locked=-1.0)
            elif k == "governor_mode":
                st.update(u=s.u_idx[it["unit"]], mode="interconnected", big_since=None)
            elif k == "cable_centre":
                st.update(ks={s.br_idx[c] for c in it["cables"]}, locked={})
            elif k == "coal_auxiliaries":
                st.update(units={s.u_idx[u]: u for u in it["units"]}, board={})
            elif k == "sync_condenser":
                st.update(b=s.idx[it["bus"]], cen={s.idx[b] for b in it["buses"]}, state="off", ready=None)
            elif k == "load_restoration":
                st.update(buses={s.idx[b]: True for b in it["buses"]}, live_since={}, locked={},
                          at={s.idx[b]: [f for f in range(s.nf) if s.fd_bus[f] == s.idx[b]] for b in it["buses"]})
            elif k == "house_load":
                u = s.u_idx[it["unit"]]
                # only a unit that was running at the blackout and came through it can hold house load
                st.update(u=u, on=s.state[u] == "off" and u not in s.damaged and not s.s["offline_h"].get(u))
            # version 5
            if k == "hydro_cascade":
                st["valley"] = {s.f_idx[f] for f in it.get("valley_feeders", [])}
            elif k == "dam_crew":
                st.update(u=s.u_idx[it["unit"]], b=s.idx[it["bus"]])
            elif k == "hospital_transfer":
                st.update(f={s.f_idx[f] for f in it["feeders"]}, diesel={s.f_idx[f] for f in it["feeders"]},
                          hold=set(), release={}, back={})
            elif k == "interruptible_contracts":
                st.update(f={s.f_idx[f] for f in it["feeders"]}, called=None, off={}, armed=set())
            elif k == "converter_overvoltage":
                st.update(b=s.idx[it["bus"]])
            elif k in ("steam_soak", "chp_steam"):
                us = it["units"] if k == "steam_soak" else [it["unit"]]
                st.update(units={s.u_idx[u]: u for u in us}, heat={}, until={})
            elif k == "motor_voltage":
                st.update(seq=[s.f_idx[f] for f in it["sequence"]])
            elif k == "tap_changers":
                st.update(buses={s.idx[b] for b in it["buses"]}, f=np.array([s.f_idx[f] for f in it["feeders"]], int),
                          blocked={}, again={})
            elif k == "dual_fuel":
                st.update(units={s.u_idx[u]: u for u in it["units"]}, ccgt=[s.u_idx[u] for u in it["ccgt"]],
                          oil=value(it, "oil_mwh"), fuel={s.u_idx[u]: "gas" for u in it["units"]}, switching={})
            elif k == "rural_switching":
                st.update(buses={s.idx[b] for b in it["buses"]})
            elif k == "ferranti_line":
                st.update(cen={s.idx[b] for b in it["buses"]}, first=None, locked=-1.0)
                st["ks"] = [k2 for k2 in range(s.nl) if (s.bf[k2] in st["cen"]) != (s.bt[k2] in st["cen"])]
            elif k == "sympathetic_inrush":
                st.update(cen={s.idx[b] for b in it["buses"]}, live_at={}, locked={})
            elif k == "coal_mills":
                st.update(units={s.u_idx[u]: u for u in it["units"]}, mills={s.u_idx[u]: [] for u in it["units"]},
                          last={})
            elif k == "water_pumping":
                st.update(f={s.f_idx[f] for f in it["feeders"]}, last=-1e9, locked={})
            elif k == "reclosers":
                st.update(f={s.f_idx[f] for f in it["feeders"]}, trips={}, locked=set(), split=set(), crew={})
            elif k == "smart_loads":
                st["f"] = np.array([s.f_idx[f] for f in it["feeders"]], int)
        # where each crew is and since when (local control after a takeover)
        self.arrived = {c: (C["at"], 0.0) for c, C in enumerate(s.crews)}
        self.pmax0 = s.u_pmax.copy()
        self.kind = {}
        for iid, it in self.items.items():
            self.kind.setdefault(it["kind"], []).append(iid)

    # ------------------------------------------------------------------ helpers
    def _one(self, kind):
        ids = self.kind.get(kind)
        return (self.items[ids[0]], self.st[ids[0]]) if ids else (None, None)

    def _reserve(self, lab):
        """Governor headroom (MW) online in an island: what its units can pick up within seconds."""
        s = self.sim
        return float(sum(min(s.u_gov[u] * s.u_pmax[u], s.u_pmax[u] - s.pm[u]) for u in s._island_units(lab)))

    def _hz_ok(self, bus, band=0.1):
        s = self.sim
        hz = float(s.f_bus[bus])
        return math.isfinite(hz) and abs(hz - 60.0) <= band, hz

    def _trip_feeders(self, fs):
        s = self.sim
        n = 0
        for f in fs:
            if s.fd_on[f]:
                s.fd_on[f] = False
                n += 1
        if n:
            s.need_pf = True
        return n

    def _local(self, bus, need):
        """A crew has had local control at this bus for `need` minutes (it arrived that long ago and is there)."""
        s = self.sim
        for c, C in enumerate(s.crews):
            if C["to"] is None and C["at"] == bus:
                at, t0 = self.arrived.get(c, (None, s.t))
                if at == bus and s.t - t0 >= need - 1e-9:
                    return True
        return False

    def unit_cmd_refused(self, u):
        it, st = self._one("dam_crew")
        if it is not None and u == st["u"] and not self._local(st["b"], value(it, "takeover_min")):
            return f"no control at the dam: a crew at {it['bus']} must take over local control first"
        return None

    def remote_override(self, bus):
        """Remote control and telemetry at a bus, where an item decides it (None: the usual rule)."""
        it, st = self._one("rural_switching")
        if it is not None and bus in st["buses"]:
            return self._local(bus, value(it, "local_min"))
        return None

    def on_start(self, u, heat):
        for kind in ("steam_soak", "chp_steam"):
            it, st = self._one(kind)
            if it is not None and u in st["units"]:
                st["heat"][u] = heat
                st["until"].pop(u, None)

    def soaking(self, u):
        it, st = self._one("steam_soak")
        return it is not None and u in st["until"] and self.sim.t < st["until"][u]

    @staticmethod
    def _mult(heat):
        return {"hot": 1.0, "warm": 2.0}.get(heat, 3.0)

    def _interlock(self, it, st, f, why):
        """The industrial process interlock trips the complex."""
        s = self.sim
        st["locked"] = s.t + value(it, "lockout_min")
        n = self._trip_feeders(g for g in st["seq"] if g != f)
        return (f"process interlock tripped the industrial complex ({why}; {n + 1} feeder(s) off); it can restart "
                f"from {s.g['loads'][st['seq'][0]]['id']} at {st['locked']:.0f} min")

    # ------------------------------------------------------------------ checks the simulator calls
    def close_refused(self, k, section=None):
        """Before a breaker is closed onto dead network (section given) or at all: why not, or None."""
        fl, fst = self._one("ferranti_line")
        if fl is not None and k in fst["ks"] and self.sim.t < fst["locked"]:
            return f"locked out by the centre lines' overvoltage relays until {fst['locked']:.0f} min"
        it, st = self._one("cable_centre")
        if it is None or section is None:
            return None
        s = self.sim
        cables = [c for c in st["ks"] if c == k or (s.closed[c] and s.bf[c] in section and s.bt[c] in section)]
        if not cables:
            return None
        for c in cables:
            if s.t < st["locked"].get(c, -1.0):
                return (f"cable {s.g['branches'][c]['id']} is locked out by its protection until "
                        f"{st['locked'][c]:.0f} min")
        return None

    def energise_refused(self, k, section, cap):
        """The section is about to be energised from an island with `cap` MW online: why it trips, or None."""
        s = self.sim
        it, st = self._one("cable_centre")
        if it is not None:
            cables = [c for c in st["ks"] if c == k or (s.closed[c] and s.bf[c] in section and s.bt[c] in section)]
            if cables and (len(cables) > 1 or cap < value(it, "strength_mw")):
                for c in cables:
                    st["locked"][c] = s.t + it["lockout_min"]
                return (f"cable overvoltage protection tripped ({len(cables)} cable(s) energised from an island with "
                        f"{cap:.0f} MW online); locked out until {s.t + it['lockout_min']:.0f} min")
        it, st = self._one("sync_condenser")
        if it is not None and st["state"] != "on" and len(set(int(i) for i in section) & st["cen"]) > 1:
            return (f"overvoltage on the centre's long lines: the synchronous condenser at {it['bus']} is not "
                    f"running")
        sec = set(int(i) for i in section)
        it, st = self._one("converter_overvoltage")
        if it is not None and st["b"] in sec and cap < value(it, "ferranti_mw"):
            return f"Ferranti overvoltage at the converter station {it['bus']} ({cap:.0f} MW online in the island)"
        it, st = self._one("ferranti_line")
        if it is not None and sec & st["cen"]:
            lab = max(s.island[s.bf[k]], s.island[s.bt[k]])
            absorb = float(sum(-s.g["units"][u]["qmin"] for u in s._island_units(lab)))
            if absorb < value(it, "absorb_mvar"):
                st["locked"] = s.t + it["lockout_min"]
                return (f"Ferranti overvoltage on the long lines into the centre ({absorb:.0f} MVAr of absorption "
                        f"online in the island); every line into the centre locked out until {st['locked']:.0f} min")
        it, st = self._one("sympathetic_inrush")
        if it is not None and sec & st["cen"]:
            sw = value(it, "settle_min")
            hit = [b for b, t0 in st["live_at"].items() if s.t - t0 < sw - 1e-9 and s.island[b] >= 0]
            for b in hit:
                fs = [f for f in range(s.nf) if s.fd_bus[f] == b]
                self._trip_feeders(fs)
                st["locked"][b] = s.t + 15.0
                s.ev(self.area_of[it["id"]], f"{it['id']}: sympathetic inrush tripped the feeders at {s.bus_id[b]} "
                                             f"(energised {s.t - st['live_at'][b]:.1f} min before); blocked until "
                                             f"{s.t + 15:.0f} min")
        return None

    def start_refused(self, u):
        s = self.sim
        it, st = self._one("dual_fuel")
        if it is not None and u in st["ccgt"]:
            g = self._one("gas_linepack")[1]
            if g is not None and 100 * g["pack"] / g["full"] < value(it, "trip_pct"):
                return "gas pressure too low for the combined cycle"
        for iid in self.kind.get("gas_linepack", []):
            st = self.st[iid]
            if u in st["units"] and s.t < st["locked"]:
                return f"low gas pressure: no start before {st['locked']:.0f} min"
        it, st = self._one("coal_auxiliaries")
        if it is not None and u in st["units"]:
            b = st["board"].get(u)
            if b is None:
                return "its auxiliary board is not fed (equip ... board true)"
            if s.t - b < value(it, "purge_min") - 1e-9:
                return f"boiler purge and fill not complete (board fed since {b:.0f} min)"
        return None

    def start_now(self, u):
        """A unit that goes online within the step instead of its start time (house load). Returns its first MW."""
        it, st = self._one("house_load")
        if it is None or u != st["u"] or not st["on"]:
            return None
        s = self.sim
        if s.island[s.u_bus[u]] < 0:
            raise ValueError(f"{it['unit']} on house load: its bus must be live to resynchronise")
        st["on"] = False
        return value(it, "block") * s.u_pmax[u]

    def can_black_start(self, u):
        it, st = self._one("retrofit_crank")
        return it is not None and u == st["u"] and st["ready"] is not None and \
            st["ready"] <= self.sim.t <= st["until"]

    def dead_bus_start(self, u):
        """None if unit u may start with its bus dead, else why not (and the retrofit diesel starts)."""
        it, st = self._one("retrofit_crank")
        if it is None or u != st["u"]:
            return "needs power at its bus to start (it is not a black-start unit)"
        t = self.sim.t
        if st["ready"] is None or t > st["until"]:
            st["ready"] = t + value(it, "prep_min")
            st["until"] = st["ready"] + value(it, "run_min")
            return f"black-start diesel starting: the unit can start on a dead bus from {st['ready']:.0f} min"
        if t < st["ready"]:
            return f"black-start diesel not ready until {st['ready']:.0f} min"
        return None

    def dead_start_limit(self, u):
        """The MVA a unit coming online on a dead bus can energise, if its equipment limits it."""
        it, st = self._one("retrofit_crank")
        if it is not None and u == st["u"]:
            return value(it, "crank_mva")
        return None

    def pickup_refused(self, f):
        s = self.sim
        b = int(s.fd_bus[f])
        it, st = self._one("hydro_cascade")
        if it is not None and f in st.get("valley", ()):
            low = st["low"]
            if s.state[low] != "online" or s.island[s.u_bus[low]] != s.island[b]:
                return f"voltage too low in the valley: {it['lower']} is not online in its island"
        it, st = self._one("interruptible_contracts")
        if it is not None and f in st["f"]:
            if st["called"] is None or s.t < st["called"] + value(it, "notice_min") - 1e-9:
                return "the customer has not been called, or its plant is not ready yet"
            if s.t < st["off"].get(f, -1.0):
                return f"the customer's process restarts; it takes supply again from {st['off'][f]:.0f} min"
        it, st = self._one("sympathetic_inrush")
        if it is not None and s.t < st["locked"].get(b, -1.0):
            return f"blocked after a sympathetic inrush trip until {st['locked'][b]:.0f} min"
        it, st = self._one("water_pumping")
        if it is not None and f in st["f"]:
            if s.t < st["locked"].get(f, -1.0):
                return f"its pumps' motors stalled; closed again from {st['locked'][f]:.0f} min"
            v = abs(s.V[b]) if s.island[b] >= 0 else 0.0
            c_it, c_st = self._one("sync_condenser")
            cond = c_it is not None and c_st["state"] == "on" and s.island[c_st["b"]] == s.island[b]
            why = None
            if s.t - st["last"] < value(it, "stagger_min") - 1e-9:
                why = f"pressure surge, {s.t - st['last']:.1f} min after the district's last pickup"
            elif v < value(it, "v_start") - 1e-9 and not cond:
                why = f"motors stalled at {v:.3f} pu without the condenser"
            if why:
                st["locked"][f] = s.t + it["lockout_min"]
                return f"{why}; closed again from {st['locked'][f]:.0f} min"
            st["last"] = s.t
        it, st = self._one("reclosers")
        if it is not None and f in st["locked"]:
            return "its recloser is locked out (a crew resets it)"
        mv, _ = self._one("motor_voltage")
        ip, ist = self._one("industrial_process")
        if mv is not None and ip is not None and f in ist["seq"] and s.t >= ist["locked"]:
            v = abs(s.V[b]) if s.island[b] >= 0 else 0.0
            if v < value(mv, "v_start") - 1e-9:
                return self._interlock(ip, ist, f, f"motors could not start at {v:.3f} pu")
        it, st = self._one("urban_network")
        if it is not None and f in st["f"] and s.t < st["locked"]:
            return f"blocked by the centre's protection until {st['locked']:.0f} min"
        it, st = self._one("undervoltage_relays")
        if it is not None and f in st["f"] and s.t < st["done"].get(f, -1.0):
            return f"blocked by its undervoltage relay until {st['done'][f]:.0f} min"
        it, st = self._one("district_heating")
        if it is not None and f in st["f"] and s.t < st["done"].get(f, -1.0):
            return f"its distribution transformers are cooling after an overload trip until {st['done'][f]:.0f} min"
        it, st = self._one("protection_reset")
        if it is not None and f in st["f"]:
            b = int(s.fd_bus[f])
            if st["done"].get(b) is None or s.t < st["done"][b]:
                return (f"tripped at once: the protection at {s.bus_id[b]} is still in its post-blackout settings "
                        f"(a crew must restore them)")
        it, st = self._one("nuclear_aux")
        if it is not None and f == st["f"] and s.t < st["locked"]:
            return f"the station's boards are locked out until {st['locked']:.0f} min"
        it, st = self._one("load_restoration")
        if it is not None and s.t < st["locked"].get(int(s.fd_bus[f]), -1.0):
            return f"the substation's transformer protection blocks it until {st['locked'][int(s.fd_bus[f])]:.0f} min"
        it, st = self._one("industrial_process")
        if it is not None and f in st["seq"]:
            return self._process_pickup(it, st, f)
        return None

    def _process_pickup(self, it, st, f):
        s = self.sim
        if s.t < st["locked"]:
            return f"the process interlock holds the complex off until {st['locked']:.0f} min"
        i = st["seq"].index(f)
        why = None
        before = st["seq"][:i]
        lab = s.island[s.fd_bus[f]]
        cap = s._online_cap(lab) if lab >= 0 else 0.0
        ok, hz = self._hz_ok(s.fd_bus[f])
        mw = float(s._demand_mw(s.t)[f])
        gap = s.t - st["last"]
        if any(not (s.fd_on[g] and s.island[s.fd_bus[g]] >= 0) for g in before):
            why = "out of order"
        elif gap < value(it, "gap_min") - 1e-9:
            why = f"{gap:.1f} min after the previous step"
        elif not ok or cap < value(it, "ratio") * mw:
            why = f"motors stalled ({mw:.0f} MW into {cap:.0f} MW online at {hz:.2f} Hz)"
        if why is None:
            st["last"] = s.t
            return None
        return self._interlock(it, st, f, why)

    def on_pickup(self, f, mw):
        """A feeder came back carrying mw (its draw at pickup)."""
        s = self.sim
        it, st = self._one("hospital_transfer")
        if it is not None and f in st["diesel"]:
            return True                     # on its own diesel: nothing drawn until its transfer
        it, st = self._one("reclosers")
        if it is not None and f in st["f"]:
            m = mw / 2.0 if f in st["split"] else mw
            if m > value(it, "setting") * s.fd_mw[f] + 1e-9:
                return self._recloser_trip(it, st, f, m)
        for iid in self.kind.get("urban_network", []):
            it, st = self.items[iid], self.st[iid]
            if f not in st["f"]:
                continue
            w = value(it, "window_min")
            st["picks"] = [(t, g, m) for t, g, m in st["picks"] if s.t - t < w - 1e-9] + [(s.t, f, mw)]
            tot = sum(m for _, _, m in st["picks"])
            if tot > value(it, "block_mw"):
                fs = [g for _, g, _ in st["picks"]]
                n = self._trip_feeders(fs)
                st["picks"] = []
                st["locked"] = s.t + it["lockout_min"]
                s.ev(self.area_of[iid], f"{iid}: overcurrent protection tripped {n} centre feeder(s) ({tot:.0f} MW "
                                        f"picked up in the last {w:.0f} min or less); centre pickup blocked until "
                                        f"{st['locked']:.0f} min")
                return False
        return True

    def _recloser_trip(self, it, st, f, m):
        s = self.sim
        s.fd_on[f] = False
        s.need_pf = True
        st["trips"][f] = st["trips"].get(f, 0) + 1
        lock = st["trips"][f] >= 3
        if lock:
            st["locked"].add(f)
        s.ev(self.area_of[it["id"]], f"{it['id']}: recloser tripped {s.g['loads'][f]['id']} ({m:.1f} MW at pickup)"
                                     + ("; LOCKED OUT (a crew resets it)" if lock else ""))
        return False

    def offsite_ok(self, uid):
        """Whether a nuclear plant's cooling has off-site power (None: the usual rule, its aux feeder served)."""
        it, st = self._one("nuclear_aux")
        if it is None or uid != it["plant"]:
            return None
        return st["handed"]

    def gov_factors(self, units):
        """Multipliers on the governor reserve and time constant of these units."""
        g = [1.0] * len(units)
        tg = [1.0] * len(units)
        it, st = self._one("governor_mode")
        if it is not None and st["u"] in units and st["mode"] == "interconnected":
            j = list(units).index(st["u"])
            g[j], tg[j] = 1.0 / 3.0, 3.0
        return g, tg

    def adjust_draw(self, draw, on):
        """Equipment's effect on what the feeders draw (MW array, in place)."""
        s = self.sim
        it, st = self._one("nuclear_aux")
        if it is not None and on[st["f"]] and st["stage"] >= 2:
            draw[st["f"]] += sum(it["stage_mw"][1:st["stage"]])
        it, st = self._one("hospital_transfer")
        if it is not None:
            for f in st["diesel"]:
                draw[f] = 0.0
        it, st = self._one("tap_changers")
        if it is not None:
            fs = st["f"][on[st["f"]]]
            if fs.size:
                v = np.abs(s.V[s.fd_bus[fs]])
                blk = np.array([int(s.fd_bus[f]) in st["blocked"] for f in fs])
                rec = 1.0 + value(it, "recovery") * np.clip((1.0 - v) / 0.03, 0.0, 1.0)
                draw[fs] *= np.where(blk, np.minimum(v, 1.0) ** 2, rec)
        it, st = self._one("smart_loads")
        if it is not None:
            fs = st["f"][on[st["f"]] & (s.fd_served_since[st["f"]] >= 0)]
            if fs.size:
                age = s.t - s.fd_served_since[fs] - value(it, "resync_min")
                k = np.where(age >= -1e-9, np.exp(-np.maximum(age, 0.0) / it["decay_min"]), 0.0)
                draw[fs] += value(it, "surge") * s._demand_mw(s.t)[fs] * k
        it, st = self._one("reclosers")
        if it is not None and st["split"]:
            for f in st["split"]:
                if on[f] and s.fd_served_since[f] >= 0 and s.t - s.fd_served_since[f] < 5.0 - 1e-9:
                    draw[f] *= 0.5
        return draw

    def extra_loads(self):
        """(bus, MW) drawn at buses by equipment itself."""
        s = self.sim
        out = []
        it, st = self._one("coal_auxiliaries")
        if it is not None:
            for u, since in st["board"].items():
                out.append((int(s.u_bus[u]), value(it, "aux_share") * s.u_pmax[u]))
        it, st = self._one("sync_condenser")
        if it is not None and st["state"] == "starting":
            out.append((st["b"], value(it, "start_mw")))
        it, st = self._one("coal_mills")
        if it is not None:
            for u, ms in st["mills"].items():
                if ms:
                    out.append((int(s.u_bus[u]), len(ms) * value(it, "mill_mw")))
        return out

    def injection(self, buses):
        """MW the HVDC link injects into an island (a set of bus indices), less what equipment draws there."""
        p = sum(st["p"] for iid in self.kind.get("lcc_hvdc", []) for st in [self.st[iid]] if st["b"] in buses)
        return p - sum(mw for b, mw in self.extra_loads() if b in buses)

    def injections(self):
        """(bus, MW, MVAr) equipment injects for the power flow (negative: drawn)."""
        out = [(b, -mw, 0.0) for b, mw in self.extra_loads()]
        for iid in self.kind.get("lcc_hvdc", []):
            it, st = self.items[iid], self.st[iid]
            q = st["banks"] * it["bank_mvar"] - it["q_per_mw"] * st["p"]
            if st["p"] > 0 or st["banks"]:
                out.append((st["b"], st["p"], q))
        return out

    # ------------------------------------------------------------------ commands
    def command(self, a, act):
        iid = act.get("id")
        it = self.items.get(iid)
        if it is None or self.area_of[iid] != a:
            raise ValueError(f"equip: {iid} is not an item of your area")
        s, st, k = self.sim, self.st[iid], it["kind"]
        if k == "lcc_hvdc":
            if "banks" in act:
                n = int(act["banks"])
                if not 0 <= n <= it["banks"]:
                    raise ValueError(f"equip: banks must be 0 to {it['banks']}")
                if n != st["banks"]:
                    st["banks"] = n
                    s.need_pf = True
            if "mw" in act:
                mw = float(act["mw"])
                if not math.isfinite(mw):
                    raise ValueError("equip: mw must be a number")
                st["target"] = min(max(mw, 0.0), it["mw_max"])
        elif k == "nuclear_aux":
            n = int(act.get("stage", -1))
            if n != st["stage"] + 1 or n not in (2, 3):
                raise ValueError(f"equip: the next stage is {st['stage'] + 1}" if st["stage"] >= 1 else
                                 "equip: stage 1 (the station-service feeder live and closed) comes first")
            hold = s.t - st["since"]
            lab = s.island[s.fd_bus[st["f"]]]
            res = self._reserve(lab) if lab >= 0 else 0.0
            ok, hz = self._hz_ok(s.fd_bus[st["f"]])
            need = value(it, "reserve_mw") * (1.0 if n == 2 else 1.5)
            ic, ist = self._one("interruptible_contracts")
            if ic is not None and res < need and lab >= 0 and hold >= value(it, "hold_min") - 1e-9 and ok:
                draw = s._feeder_draw(s._demand_mw(s.t))[0]
                armed = sorted((f for f in ist["f"] if s.fd_on[f] and s.island[s.fd_bus[f]] == lab),
                               key=lambda f: -draw[f])
                if res + sum(draw[f] for f in armed) >= need:
                    shed = []
                    for f in armed:
                        if res >= need:
                            break
                        shed.append(f)
                        res += float(draw[f])
                    self._trip_feeders(shed)
                    s.ev(a, f"{ic['id']}: {len(shed)} interruptible feeder(s) tripped to start {it['plant']}'s pumps")
            if hold < value(it, "hold_min") - 1e-9 or not ok or res < need:
                st.update(stage=0, since=None, locked=s.t + value(it, "lockout_min"))
                s.fd_on[st["f"]] = False
                s.need_pf = True
                s.ev(a, f"{iid}: stage {n} TRIPPED the station's boards ({hold:.0f} min at stage {n - 1}, "
                        f"{res:.0f} MW of governor headroom online, {hz:.2f} Hz); {it['feeder']} opened, locked out "
                        f"until {st['locked']:.0f} min")
            else:
                st.update(stage=n, since=s.t)
                s.need_pf = True
                s.ev(a, f"{iid}: stage {n} running")
        elif k == "protection_reset":
            c = s.crew_idx.get(act.get("crew"))
            b = s.idx.get(act.get("bus"))
            if c is None or s.crews[c]["area"] != a or b is None or b not in st["bus"].values():
                raise ValueError("equip: needs your crew and a centre substation")
            C = s.crews[c]
            if C["to"] is not None or C["at"] != b:
                raise ValueError(f"equip: {C['id']} is not at {s.bus_id[b]}")
            if C["task"]:
                raise ValueError(f"equip: {C['id']} is busy until {C['task_end']:.0f} min")
            if st["done"].get(b) is not None:
                raise ValueError(f"equip: {s.bus_id[b]} is done or under way")
            st["done"][b] = s.t + value(it, "work_min")
            st.setdefault("crew", {})[b] = c
            C.update(task="reset", task_end=st["done"][b], task_target=None)
            s.ev(a, f"{C['id']} restoring the protection settings at {s.bus_id[b]}")
        elif k == "governor_mode":
            m = act.get("mode")
            if m not in ("isolated", "interconnected"):
                raise ValueError("equip: mode is isolated or interconnected")
            st["mode"] = m
            st["big_since"] = None
        elif k == "coal_auxiliaries":
            u = s.u_idx.get(act.get("unit"))
            if u not in st["units"]:
                raise ValueError("equip: not one of the item's coal units")
            if bool(act.get("board")):
                if s.island[s.u_bus[u]] < 0:
                    raise ValueError(f"equip: {act['unit']}'s bus is dead")
                st["board"].setdefault(u, s.t)
            else:
                st["board"].pop(u, None)
            s.need_pf = True
        elif k == "sync_condenser":
            if bool(act.get("on")):
                if st["state"] != "off":
                    return
                lab = s.island[st["b"]]
                if lab < 0:
                    raise ValueError(f"equip: {it['bus']} is dead")
                head = sum(s.u_pmax[v] - s.pm[v] for v in s._island_units(lab))
                if head < value(it, "start_mw"):
                    raise ValueError(f"equip: the condenser's start needs more headroom than the island's "
                                     f"{head:.0f} MW")
                st.update(state="starting", ready=s.t + value(it, "runup_min"))
                s.ev(a, f"{iid} running up")
            else:
                st.update(state="off", ready=None)
            s.need_pf = True
        elif k == "load_restoration":
            b = s.idx.get(act.get("bus"))
            if b not in st["buses"]:
                raise ValueError("equip: not a centre substation")
            if not s._remote_ok(b):
                raise ValueError(f"equip: no remote control at {s.bus_id[b]}")
            st["buses"][b] = bool(act.get("armed"))
        elif k == "hospital_transfer":
            b = s.idx.get(act.get("bus"))
            if b is None or s.bus_id[b] not in it["buses"]:
                raise ValueError("equip: not a substation with hospital feeders")
            if bool(act.get("hold")):
                st["hold"].add(b)
            elif b in st["hold"]:
                st["hold"].discard(b)
                st["release"][b] = s.t
        elif k == "interruptible_contracts":
            if bool(act.get("call")) and st["called"] is None:
                st["called"] = s.t
                s.ev(a, f"{iid}: customers called; ready from {s.t + value(it, 'notice_min'):.0f} min")
        elif k == "tap_changers":
            b = s.idx.get(act.get("bus"))
            if b not in st["buses"]:
                raise ValueError("equip: not a centre substation")
            if bool(act.get("blocked")):
                if b not in st["blocked"]:
                    if s.t < st["again"].get(b, -1.0):
                        raise ValueError(f"equip: tap changers at {s.bus_id[b]} can be blocked again from "
                                         f"{st['again'][b]:.0f} min")
                    st["blocked"][b] = s.t + value(it, "block_max_min")
            elif b in st["blocked"]:
                del st["blocked"][b]
                st["again"][b] = s.t + value(it, "block_max_min")
            s.need_pf = True
        elif k == "dual_fuel":
            u = s.u_idx.get(act.get("unit"))
            fuel = act.get("fuel")
            if u not in st["units"] or fuel not in ("gas", "oil"):
                raise ValueError("equip: one of the item's gas turbines, and fuel gas or oil")
            if u in st["switching"]:
                raise ValueError(f"equip: {act['unit']} is already switching")
            if fuel != st["fuel"][u]:
                if fuel == "oil" and st["oil"] <= 0:
                    raise ValueError("equip: the oil tank is empty")
                st["switching"][u] = (fuel, s.t + value(it, "switch_min"))
                s.ev(a, f"{iid}: {act['unit']} switching to {fuel}")
        elif k == "coal_mills":
            u = s.u_idx.get(act.get("unit"))
            if u not in st["units"]:
                raise ValueError("equip: not one of the item's coal units")
            n = int(act.get("mills", -1))
            ms = st["mills"][u]
            if not 0 <= n <= it["mills"][st["units"][u]]:
                raise ValueError("equip: mills must be 0 to 5")
            if n > len(ms):
                if s.state[u] != "online":
                    raise ValueError(f"equip: {st['units'][u]} is not running")
                b_it, b_st = self._one("coal_auxiliaries")
                if b_it is not None and u not in b_st["board"]:
                    raise ValueError(f"equip: {st['units'][u]}'s auxiliary board is not fed")
                if n > len(ms) + 1 or st["last"].get(u) == s.step_i:
                    raise ValueError("equip: one more mill a step at most")
                ms.append(s.t)
                st["last"][u] = s.step_i
            else:
                del ms[n:]
            s.need_pf = True
        elif k == "reclosers":
            c = s.crew_idx.get(act.get("crew"))
            f = s.f_idx.get(act.get("feeder"))
            if c is None or s.crews[c]["area"] != a or f not in st["f"]:
                raise ValueError("equip: needs your crew and a feeder of the area")
            C = s.crews[c]
            if C["to"] is not None or C["at"] != s.fd_bus[f]:
                raise ValueError(f"equip: {C['id']} is not at {s.bus_id[s.fd_bus[f]]}")
            if C["task"]:
                raise ValueError(f"equip: {C['id']} is busy until {C['task_end']:.0f} min")
            C.update(task="split", task_end=s.t + it["split_min"], task_target=None)
            st["crew"][c] = f
            s.ev(a, f"{C['id']} splitting {act['feeder']} until {C['task_end']:.0f} min")
        else:
            raise ValueError(f"equip: {iid} takes no commands")

    # ------------------------------------------------------------------ dynamics, once a step
    def step(self):
        s = self.sim
        dt_h = 0.5 / 60.0
        for iid, it in self.items.items():
            st, k, a = self.st[iid], it["kind"], self.area_of[iid]
            if k == "hydro_cascade":
                st["hist"].append(float(s.pm[st["up"]]))
                lag = int(round(value(it, "travel_min") * 2))
                inflow = (st["hist"][0] if len(st["hist"]) > lag else 0.0) + it["inflow_mw"]
                while len(st["hist"]) > lag:
                    st["hist"].popleft()
                st["level"] = min(value(it, "pond_mwh"), st["level"] + (inflow - float(s.pm[st["low"]])) * dt_h)
                if st["level"] <= 0:
                    st["level"] = 0.0
                    u = st["low"]
                    if s.state[u] in ("online", "starting"):
                        s._stop_unit(u, trip=True)
                        s.lock_until[u] = s.t + it["lockout_min"]
                        s.ev(a, f"{it['lower']} TRIPPED on low head (head pond empty); restart from "
                                f"{s.lock_until[u]:.0f} min")
                        s._components()
            elif k == "gas_linepack":
                fed = self._fed_since(st["f"])
                st["comp"] = fed is not None and fed >= value(it, "warmup_min")
                df_it, df = self._one("dual_fuel")
                oil = df["fuel"] if df_it is not None else {}
                burn = float(sum(s.pm[u] for u in st["units"] if s.state[u] == "online" and oil.get(u) != "oil"))
                st["pack"] = min(st["full"], st["pack"] + ((value(it, "refill_mw") if st["comp"] else 0.0)
                                                           - burn) * dt_h)
                if st["pack"] <= 0:
                    st["pack"] = 0.0
                    hit = [u for u in st["units"] if s.state[u] in ("online", "starting") and oil.get(u) != "oil"]
                    for u in hit:
                        s._stop_unit(u, trip=True)
                    if hit:
                        st["locked"] = s.t + it["lockout_min"]
                        s.ev(a, f"{iid}: LOW GAS PRESSURE, the pipeline is empty: "
                                f"{', '.join(s.g['units'][u]['id'] for u in hit)} tripped; no start before "
                                f"{st['locked']:.0f} min")
                        s._components()
            elif k == "lcc_hvdc":
                p0 = st["p"]
                lab = s.island[st["b"]]
                if lab < 0 or s.t < st["blocked"] or st["target"] <= 0:
                    st["p"] = 0.0
                else:
                    cap = s._online_cap(lab)
                    mn = value(it, "min_mw")
                    if st["p"] <= 0:
                        if st["banks"] >= 1:
                            st["p"] = mn
                    else:
                        step = it["ramp_mw_min"] * 0.5
                        st["p"] = max(mn, st["p"] + max(-step, min(step, st["target"] - st["p"])))
                    if st["p"] > 0 and (cap < value(it, "scr") * st["p"] or
                                        st["p"] > value(it, "bank_mw") * st["banks"] + 1e-9):
                        why = "weak AC side" if cap < value(it, "scr") * st["p"] else "too few filter banks"
                        st["blocked"] = s.t + it["block_min"]
                        dist = [s.f_idx[f] for f in it.get("district_feeders", [])]
                        n = self._trip_feeders(f for f in dist if s.island[s.fd_bus[f]] == lab)
                        if n:
                            s.ev(a, f"{iid}: the voltage dip tripped {n} district feeder(s)")
                        s.ev(a, f"{iid}: COMMUTATION FAILURE ({st['p']:.0f} MW, {st['banks']} bank(s), {cap:.0f} MW "
                                f"online in the island: {why}); import lost, link blocked until "
                                f"{st['blocked']:.0f} min")
                        st["p"] = 0.0
                if st["p"] != p0:
                    s.need_pf = True
                    if p0 == 0 and st["p"] > 0:
                        s.ev(a, f"{iid}: converter deblocked at {st['p']:.0f} MW")
            elif k == "retrofit_crank":
                if st["until"] is not None and s.t > st["until"] and st["ready"] is not None:
                    if s.state[st["u"]] == "off" and s.island[s.u_bus[st["u"]]] < 0:
                        s.ev(a, f"{iid}: black-start diesel stopped (day tank empty)")
                    st["ready"] = st["until"] = None
            elif k == "nuclear_aux":
                f = st["f"]
                fed = s.fd_on[f] and s.island[s.fd_bus[f]] >= 0
                if not fed:
                    if st["stage"] > 0 and not st["handed"]:
                        s.ev(a, f"{iid}: supply to the station's boards lost at stage {st['stage']}; the stages start "
                                f"again")
                    st.update(stage=0, since=None)
                elif st["stage"] == 0:
                    st.update(stage=1, since=s.t)
                    s.ev(a, f"{iid}: stage 1 (station service) fed")
                elif st["stage"] == 3 and not st["handed"] and s.t - st["since"] >= value(it, "handover_min") - 1e-9:
                    st["handed"] = True
                    s.ev(a, f"{iid}: cooling handed over to off-site power")
            elif k == "district_heating":
                u = st["u"]
                at = s.state[u] == "online" and s.pm[u] >= value(it, "must_run") * s.u_pmax[u] - 0.5
                if at:
                    st["low_since"] = None
                    if st["since"] is None:
                        st["since"] = s.t
                    if not st.get("warm") and s.t - st["since"] >= value(it, "heat_min") - 1e-9:
                        st["warm"] = True
                        s.need_pf = True
                        s.ev(a, f"{iid}: district heat restored to the centre")
                else:
                    st["since"] = None
                    if st.get("warm"):
                        if st["low_since"] is None:
                            st["low_since"] = s.t
                        elif s.t - st["low_since"] >= 10.0 - 1e-9:
                            st["warm"] = False
                            s.ev(a, f"{iid}: district heat lost; the centre heats with electricity again")
                if not st.get("warm"):
                    tm = value(it, "trip_min")
                    hit = [f for f in st["f"] if s.fd_on[f] and s.island[s.fd_bus[f]] >= 0
                           and s.fd_served_since[f] >= 0 and s.t - s.fd_served_since[f] >= tm - 1e-9]
                    if hit:
                        self._trip_feeders(hit)
                        for f in hit:
                            st["done"][f] = s.t + 60.0
                        s.ev(a, f"{iid}: {len(hit)} centre feeder(s) tripped on transformer overload (electric "
                                f"heating, no district heat); closed again from {s.t + 60:.0f} min")
            elif k == "undervoltage_relays":
                vt = value(it, "v_trip")
                hit = [f for f in st["f"] if s.fd_on[f] and s.island[s.fd_bus[f]] >= 0
                       and 0.5 < abs(s.V[s.fd_bus[f]]) < vt]
                if hit:
                    vs = sorted({round(float(abs(s.V[s.fd_bus[f]])), 3) for f in hit})
                    self._trip_feeders(hit)
                    for f in hit:
                        st["done"][f] = s.t + it["lockout_min"]
                    s.ev(a, f"{iid}: undervoltage relays tripped {len(hit)} centre feeder(s) (at {', '.join(map(str, vs))}"
                            f" pu); blocked for 15 min")
            elif k == "protection_reset":
                for b, c in list(st.get("crew", {}).items()):
                    C = s.crews[c]
                    if C["task"] != "reset" or C["at"] != b or C["to"] is not None:
                        # the crew was sent elsewhere before it finished: the work is not done
                        del st["crew"][b]
                        del st["done"][b]
                        s.ev(a, f"{iid}: work at {s.bus_id[b]} abandoned")
                    elif s.t >= C["task_end"]:
                        del st["crew"][b]
                        C["task"] = None
                        s.ev(a, f"{iid}: protection settings at {s.bus_id[b]} restored ({C['id']} free)")
            elif k == "governor_mode":
                u = st["u"]
                lab = s.island[s.u_bus[u]]
                big = (st["mode"] == "isolated" and s.state[u] == "online" and lab >= 0
                       and s._online_cap(lab) > value(it, "ratio") * s.u_pmax[u])
                if not big:
                    st["big_since"] = None
                elif st["big_since"] is None:
                    st["big_since"] = s.t
                elif s.t - st["big_since"] >= value(it, "hunt_min") - 1e-9:
                    st["big_since"] = None
                    s._stop_unit(u, trip=True)
                    ties = [k for k in range(s.nl) if s.br_tie[k] and s.closed[k] and a in s.br_owner[k]]
                    for k in ties:
                        s.closed[k] = False
                    s.ev(a, f"{it['unit']} TRIPPED on power swings: its governor in isolated mode hunted against "
                            f"{s._online_cap(lab):.0f} MW of other units; out-of-step relays opened "
                            f"{len(ties)} tie line(s)")
                    s._components()
            elif k == "coal_auxiliaries":
                for u in list(st["board"]):
                    if s.island[s.u_bus[u]] < 0:
                        del st["board"][u]
                        s.ev(a, f"{iid}: auxiliary board of {st['units'][u]} lost supply")
                        s.need_pf = True
                for u in st["board"]:
                    if s.state[u] == "off":
                        # banked: the boiler holds the temperature it had when the board was first fed
                        st.setdefault("bank", {}).setdefault(u, s.t - s.last_off[u])
                        s.last_off[u] = s.t - st["bank"][u]
                for u in list(st.get("bank", {})):
                    if u not in st["board"] or s.state[u] != "off":
                        del st["bank"][u]
                for u in st["units"]:
                    if s.state[u] in ("online", "starting") and u not in st["board"]:
                        s._stop_unit(u, trip=True)
                        s.ev(a, f"{st['units'][u]} TRIPPED: no supply to its auxiliary board")
                        s._components()
            elif k == "sync_condenser":
                if st["state"] != "off" and s.island[st["b"]] < 0:
                    st.update(state="off", ready=None)
                    s.ev(a, f"{iid} stopped: {it['bus']} lost power")
                elif st["state"] == "starting" and s.t >= st["ready"]:
                    st["state"] = "on"
                    s.need_pf = True
                    s.ev(a, f"{iid} running")
            elif k == "load_restoration":
                for b, armed in st["buses"].items():
                    if s.island[b] < 0:
                        st["live_since"].pop(b, None)
                        continue
                    since = st["live_since"].setdefault(b, s.t)
                    if armed and s.t - since >= value(it, "delay_min") - 1e-9 and s.t >= st["locked"].get(b, -1.0):
                        hit = [f for f in st["at"][b] if not s.fd_on[f]]
                        if hit:
                            for f in hit:
                                s.fd_on[f] = True
                                s._pickup(f)
                            s.need_pf = True
                            draw = float(sum(s._demand_mw(s.t)[f] * s.fd_peak[f] for f in st["at"][b] if s.fd_on[f]))
                            if draw > value(it, "transformer_mw"):
                                self._trip_feeders(st["at"][b])
                                st["locked"][b] = s.t + 60.0
                                s.ev(a, f"{iid}: automatic load restoration closed {len(hit)} feeder(s) at "
                                        f"{s.bus_id[b]} ({draw:.0f} MW with cold load): transformer overcurrent trip, "
                                        f"its feeders blocked until {st['locked'][b]:.0f} min")
                            else:
                                s.ev(a, f"{iid}: automatic load restoration closed {len(hit)} feeder(s) at "
                                        f"{s.bus_id[b]}")
            elif k == "house_load":
                if st["on"] and s.t >= value(it, "hold_min") - 1e-9:
                    st["on"] = False
                    s.last_off[st["u"]] = s.t
                    s.ev(a, f"{it['unit']} tripped off house load (boiler)")

        self.step5()

    def step5(self):
        """Version 5's second items, once a step after the first ones."""
        s = self.sim
        dt_h = 0.5 / 60.0
        for c, C in enumerate(s.crews):
            if C["to"] is None and self.arrived.get(c, (None,))[0] != C["at"]:
                self.arrived[c] = (C["at"], s.t)
            elif C["to"] is not None:
                self.arrived[c] = (None, s.t)
        served = s.fd_on & (s.island[s.fd_bus] >= 0)
        for iid, it in self.items.items():
            st, k, a = self.st[iid], it["kind"], self.area_of[iid]
            if k == "hydro_cascade" and st.get("valley"):
                low = st["low"]
                isl = s.island[s.u_bus[low]] if s.state[low] == "online" else -2
                hit = [f for f in st["valley"] if served[f] and s.island[s.fd_bus[f]] != isl]
                if hit:
                    self._trip_feeders(hit)
                    s.ev(a, f"{iid}: {len(hit)} valley feeder(s) tripped on undervoltage ({it['lower']} not online "
                            f"in their island)")
            elif k == "hospital_transfer":
                fuel_out = s.t >= value(it, "fuel_h") * 60.0 - 1e-9
                for f in sorted(st["f"]):
                    if not served[f]:
                        if not fuel_out:
                            st["diesel"].add(f)
                        st["back"].pop(f, None)
                        continue
                    if f not in st["diesel"]:
                        continue
                    b = int(s.fd_bus[f])
                    t0 = max(st["back"].setdefault(f, s.t), st["release"].get(b, -1e9))
                    due = (b not in st["hold"] and s.t - t0 >= value(it, "retransfer_min") - 1e-9) or fuel_out
                    if due:
                        st["diesel"].discard(f)
                        mw = float(s._demand_mw(s.t)[f] * (1.0 + (s.fd_peak[f] - 1.0) * math.exp(
                            -(s.t - s.fd_served_since[f]) / s.clpu_tau[f])))
                        s.need_pf = True
                        s.ev(a, f"{iid}: {s.g['loads'][f]['id']} transferred back to the grid ({mw:.0f} MW)")
                        self.on_pickup(f, mw)
            elif k == "interruptible_contracts":
                now = {f for f in st["f"] if served[f]}
                th = value(it, "trip_hz")
                for f in sorted(now):
                    hz = s.f_bus[s.fd_bus[f]]
                    if math.isfinite(hz) and hz < th:
                        s.fd_on[f] = False
                        now.discard(f)
                for f in st["armed"] - now:
                    st["off"][f] = s.t + value(it, "restart_min")
                if st["armed"] - now:
                    s.need_pf = True
                    s.ev(a, f"{iid}: {len(st['armed'] - now)} customer(s) interrupted; back from "
                            f"{s.t + value(it, 'restart_min'):.0f} min")
                st["armed"] = now
            elif k == "converter_overvoltage":
                h_it, h = self._one("lcc_hvdc")
                if h_it is not None and s.island[st["b"]] >= 0 and h["banks"]:
                    excess = h["banks"] * h_it["bank_mvar"] - h_it["q_per_mw"] * h["p"]
                    if excess > value(it, "q_margin") + 1e-9:
                        n = h["banks"]
                        h.update(banks=0, p=0.0, blocked=s.t + it["lockout_min"])
                        s.need_pf = True
                        s.ev(a, f"{iid}: OVERVOLTAGE at {it['bus']}: {n} filter bank(s) with {excess:.0f} MVAr not "
                                f"absorbed; banks tripped, link blocked until {h['blocked']:.0f} min")
            elif k in ("steam_soak", "chp_steam"):
                for u, uid in st["units"].items():
                    if s.state[u] != "online":
                        st["until"].pop(u, None)
                        if k == "chp_steam":
                            s.u_pmax[u] = self.pmax0[u]
                        continue
                    m = self._mult(st["heat"].get(u, "cold"))
                    w = value(it, "soak_min" if k == "steam_soak" else "warm_min") * m
                    until = st["until"].setdefault(u, s.online_since[u] + w)
                    if k == "chp_steam":
                        s.u_pmax[u] = self.pmax0[u] * (value(it, "gt_share") if s.t < until else 1.0)
                    elif s.t < until and s.pm[u] > value(it, "soak_share") * self.pmax0[u] + 1.0:
                        s._stop_unit(u, trip=True)
                        s.lock_until[u] = s.t + it["lockout_min"]
                        s.last_off[u] = -1e6           # its metal has to cool: the next start is cold
                        st["until"].pop(u, None)
                        s.ev(a, f"{uid} TRIPPED on rotor differential expansion (loaded to {s.pm[u]:.0f} MW during its "
                                f"soak); start again from {s.lock_until[u]:.0f} min, cold")
                        s._components()
            elif k == "motor_voltage":
                ip, ist = self._one("industrial_process")
                if ip is None or s.t < ist["locked"]:
                    continue
                low = [f for f in st["seq"] if served[f] and abs(s.V[s.fd_bus[f]]) < value(it, "v_hold") - 1e-9]
                if low:
                    f = low[0]
                    s.fd_on[f] = False
                    s.ev(a, f"{iid}: " + self._interlock(ip, ist, f, f"motors stalled at "
                                                                    f"{abs(s.V[s.fd_bus[f]]):.3f} pu"))
                    s.need_pf = True
            elif k == "tap_changers":
                for b, until in list(st["blocked"].items()):
                    if s.t >= until - 1e-9:
                        del st["blocked"][b]
                        st["again"][b] = s.t + value(it, "block_max_min")
                        s.need_pf = True
                        s.ev(a, f"{iid}: tap changers at {s.bus_id[b]} back in automatic (block time out)")
            elif k == "dual_fuel":
                for u, (fuel, t1) in list(st["switching"].items()):
                    if s.t >= t1 - 1e-9:
                        del st["switching"][u]
                        st["fuel"][u] = fuel
                        s.ev(a, f"{iid}: {st['units'][u]} now on {fuel}")
                for u in st["units"]:
                    s.u_pmax[u] = self.pmax0[u] * (0.5 if u in st["switching"] else 1.0)
                burn = float(sum(s.pm[u] for u in st["units"] if s.state[u] == "online" and st["fuel"][u] == "oil"))
                st["oil"] = max(0.0, st["oil"] - burn * dt_h)
                if st["oil"] <= 0:
                    hit = [u for u in st["units"] if st["fuel"][u] == "oil" and s.state[u] in ("online", "starting")]
                    for u in hit:
                        s._stop_unit(u, trip=True)
                    if hit:
                        s.ev(a, f"{iid}: oil tank empty: {', '.join(st['units'][u] for u in hit)} tripped")
                        s._components()
                g_it, g = self._one("gas_linepack")
                if g_it is not None and 100 * g["pack"] / g["full"] < value(it, "trip_pct"):
                    hit = [u for u in st["ccgt"] if s.state[u] in ("online", "starting")]
                    for u in hit:
                        s._stop_unit(u, trip=True)
                    if hit:
                        s.ev(a, f"{iid}: low gas pressure ({100 * g['pack'] / g['full']:.0f}%): "
                                f"{', '.join(s.g['units'][u]['id'] for u in hit)} tripped")
                        s._components()
            elif k == "ferranti_line":
                live = [b for b in st["cen"] if s.island[b] >= 0]
                if not live:
                    st["first"] = None
                    continue
                if st["first"] is None:
                    st["first"] = s.t
                    st["held"] = False
                load = None
                if not st["held"] and s.t - st["first"] >= value(it, "delay_min") - 1e-9:
                    if "fs" not in st:
                        st["fs"] = np.array([f for f in range(s.nf) if int(s.fd_bus[f]) in st["cen"]], int)
                    draw = s._feeder_draw(s._demand_mw(s.t))[0]
                    load = float((draw[st["fs"]] * served[st["fs"]]).sum())
                st["load"] = load
                if load is not None and load >= value(it, "min_mw"):
                    st["held"] = True              # loaded: the far ends' voltage is down for good
                elif load is not None:
                    ks = [k2 for k2 in st["ks"] if s.closed[k2]]
                    for k2 in ks:
                        s.closed[k2] = False
                    st["locked"] = s.t + it["lockout_min"]
                    st["first"] = None
                    s.ev(a, f"{iid}: OVERVOLTAGE on the lightly loaded lines into the centre ({load:.0f} MW): "
                            f"{len(ks)} line(s) opened, locked out until {st['locked']:.0f} min")
                    s._components()
            elif k == "sympathetic_inrush":
                for b in st["cen"]:
                    if s.island[b] >= 0:
                        st["live_at"].setdefault(b, s.t)
                    else:
                        st["live_at"].pop(b, None)
            elif k == "coal_mills":
                b_it, b_st = self._one("coal_auxiliaries")
                for u, ms in st["mills"].items():
                    if s.state[u] != "online" or (b_it is not None and u not in b_st["board"]):
                        ms.clear()
                    warm = sum(1 for t0 in ms if s.t - t0 >= value(it, "warm_min") - 1e-9)
                    pmin = float(s.u_pmin[u])
                    s.u_pmax[u] = min(self.pmax0[u], pmin + warm * (self.pmax0[u] - pmin) / 5.0)
            elif k == "reclosers":
                for c, f in list(st["crew"].items()):
                    C = s.crews[c]
                    if C["task"] != "split" or C["to"] is not None or C["at"] != s.fd_bus[f]:
                        del st["crew"][c]
                        s.ev(a, f"{iid}: split of {s.g['loads'][f]['id']} abandoned")
                    elif s.t >= C["task_end"] - 1e-9:
                        del st["crew"][c]
                        C["task"] = None
                        st["split"].add(f)
                        st["locked"].discard(f)
                        st["trips"][f] = 0
                        s.ev(a, f"{iid}: {s.g['loads'][f]['id']} split, recloser reset ({C['id']} free)")
                for f in st["split"]:
                    if served[f] and 5.0 - 1e-9 <= s.t - s.fd_served_since[f] < 5.5 - 1e-9:
                        m = 0.5 * float(s._demand_mw(s.t)[f]) * (1.0 + (s.fd_peak[f] - 1.0) * math.exp(
                            -(s.t - s.fd_served_since[f]) / s.clpu_tau[f]))
                        if m > value(it, "setting") * s.fd_mw[f] + 1e-9:
                            self._recloser_trip(it, st, f, m)
        # the coal mills set the maximum; the CHP's and dual fuel's derates too (above)

    def _fed_since(self, f):
        s = self.sim
        if s.fd_on[f] and s.island[s.fd_bus[f]] >= 0 and s.fd_served_since[f] >= 0:
            return s.t - s.fd_served_since[f]
        return None

    def pf_gens(self):
        """(bus, MVAr limit) voltage-controlling machines without active power (the running condenser)."""
        it, st = self._one("sync_condenser")
        if it is not None and st["state"] == "on":
            return [(st["b"], it["q_mvar"])]
        return []

    def observe(self, a):
        s = self.sim
        out = {}
        for it in self.by_area.get(a, []):
            iid, st, k = it["id"], self.st[it["id"]], it["kind"]
            if k == "hydro_cascade":
                dc, dst = self._one("dam_crew")
                seen = dc is None or self._local(dst["b"], value(dc, "takeover_min"))
                out[iid] = dict(pond_pct=round(100 * st["level"] / value(it, "pond_mwh"), 1) if seen else None)
            elif k == "lcc_hvdc":
                out[iid] = dict(mw=round(st["p"], 1), target=st["target"], banks=st["banks"],
                                blocked_until=st["blocked"] if s.t < st["blocked"] else None)
            elif k == "retrofit_crank":
                out[iid] = dict(diesel_ready_from=st["ready"], diesel_runs_until=st["until"])
            elif k == "nuclear_aux":
                out[iid] = dict(stage=st["stage"], since_min=st["since"], handed_over=st["handed"],
                                locked_until=st["locked"] if s.t < st["locked"] else None)
            elif k == "gas_linepack":
                out[iid] = dict(pressure_pct=round(100 * st["pack"] / st["full"], 1), compressor=bool(st.get("comp")),
                                locked_until=st["locked"] if s.t < st["locked"] else None)
            elif k == "protection_reset":
                out[iid] = dict(done=sorted(s.bus_id[b] for b, u in st["done"].items() if s.t >= u),
                                under_way=sorted(s.bus_id[b] for b, u in st["done"].items() if s.t < u))
            elif k == "governor_mode":
                out[iid] = dict(mode=st["mode"])
            elif k == "coal_auxiliaries":
                out[iid] = dict(boards={st["units"][u]: dict(fed_since=t) for u, t in st["board"].items()})
            elif k == "sync_condenser":
                out[iid] = dict(state=st["state"], running_from=st["ready"])
            elif k == "load_restoration":
                out[iid] = dict(armed=sorted(s.bus_id[b] for b, x in st["buses"].items() if x))
            elif k == "house_load":
                out[iid] = dict(house_load=st["on"])
            elif k == "district_heating":
                out[iid] = dict(heat_restored=bool(st.get("warm")))
            elif k == "urban_network":
                out[iid] = dict(blocked_until=st["locked"] if s.t < st["locked"] else None)
            elif k == "industrial_process":
                out[iid] = dict(locked_until=st["locked"] if s.t < st["locked"] else None)
            elif k == "converter_overvoltage":
                h_it, h = self._one("lcc_hvdc")
                out[iid] = dict(unabsorbed_mvar=round(h["banks"] * h_it["bank_mvar"] - h_it["q_per_mw"] * h["p"], 1)
                                if h_it is not None else None)
            elif k == "dam_crew":
                out[iid] = dict(local_control=self._local(st["b"], value(it, "takeover_min")))
            elif k == "hospital_transfer":
                out[iid] = dict(on_diesel=sorted(s.g["loads"][f]["id"] for f in st["diesel"]),
                                held=sorted(s.bus_id[b] for b in st["hold"]))
            elif k == "interruptible_contracts":
                out[iid] = dict(called_min=st["called"],
                                off_until={s.g["loads"][f]["id"]: t for f, t in st["off"].items() if s.t < t})
            elif k in ("steam_soak", "chp_steam"):
                key = "soak_until" if k == "steam_soak" else "steam_ready_min"
                out[iid] = {uid: {key: st["until"].get(u)} for u, uid in st["units"].items()}
                if k == "chp_steam":
                    u = next(iter(st["units"]))
                    out[iid][st["units"][u]]["max_mw"] = round(float(s.u_pmax[u]), 1)
            elif k == "tap_changers":
                out[iid] = dict(blocked={s.bus_id[b]: t for b, t in st["blocked"].items()},
                                again_from={s.bus_id[b]: t for b, t in st["again"].items() if s.t < t})
            elif k == "dual_fuel":
                out[iid] = dict(fuel={st["units"][u]: f for u, f in st["fuel"].items()},
                                switching={st["units"][u]: dict(to=f, until=t) for u, (f, t) in st["switching"].items()},
                                oil_pct=round(100 * st["oil"] / value(it, "oil_mwh"), 1))
            elif k == "rural_switching":
                out[iid] = dict(local_control=sorted(s.bus_id[b] for b in st["buses"]
                                                     if self._local(b, value(it, "local_min"))))
            elif k == "ferranti_line":
                out[iid] = dict(centre_live_since=st["first"],
                                locked_until=st["locked"] if s.t < st["locked"] else None)
            elif k == "sympathetic_inrush":
                out[iid] = dict(energised_at={s.bus_id[b]: t for b, t in st["live_at"].items()})
            elif k == "coal_mills":
                out[iid] = {st["units"][u]: dict(mills=len(ms), started=list(ms), max_mw=round(float(s.u_pmax[u]), 1))
                            for u, ms in st["mills"].items()}
            elif k == "water_pumping":
                out[iid] = dict(last_pickup_min=st["last"] if st["last"] > 0 else None,
                                stalled_until={s.g["loads"][f]["id"]: t for f, t in st["locked"].items() if s.t < t})
            elif k == "reclosers":
                out[iid] = dict(locked=sorted(s.g["loads"][f]["id"] for f in st["locked"]),
                                split=sorted(s.g["loads"][f]["id"] for f in st["split"]),
                                trips={s.g["loads"][f]["id"]: n for f, n in st["trips"].items() if n})
        return out
