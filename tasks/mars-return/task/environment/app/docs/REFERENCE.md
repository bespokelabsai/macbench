<!-- Made by task/build_docs.py from docs/reference/NN-*.md. Edit the fragments, not this file. -->
# Program reference

The program office owns the mission simulator and the hardware models. You do not have them. You describe the vehicle in SysML v2, the systems engineering language, and the program office compiles and builds it. You learn how your hardware behaves from the tests you order: hot fires, static fires, structural tests, wind tunnel runs, arc-jet runs, parachute drops and weighing built parts. The verifier flies your design and your flight software in the program office's simulator, with the hardware as it was really built.

Your image has:

- **The design language**: the domain library in `/opt/designlang/library/` (`MarsReturnLibrary.sysml` and `MarsReturnUnits.sysml`), and the checker `design-check`.
- **Tudat** 1.0 (TU Delft) for orbits, gravity fields, frames, ephemerides and Earth's atmosphere, with SPICE through `tudatpy.interface.spice` and the kernels in `$MARSFLIGHT_KERNELS`.
- **NASA CEA v3** (`import cea`) for ideal rocket thermochemistry.
- numpy and scipy. The Python that has them all is `/opt/conda/envs/marsflight/bin/python3`, also linked as `mfpython`.

There is no mission simulator in the image. To develop your trajectory and your flight software, build your own models from these tools, and correct them with your test results. The program office can fly your committed design and flight software end to end on its own simulator, for a price (`mission_sim`, see "The program office and R&D tests").

## The mission

Launch from Kennedy LC-39A, land at Jezero crater on Mars, spend 30 sols collecting 30 sample tubes, launch from Mars, and land the samples in the Utah Test and Training Range. You design every engine, tank, stage, heat shield, parachute and power system, pay for the hardware and the tests, and write the flight software. The vehicle is described in SysML v2 with the domain library: materials by their handbook designation, propellants by their specification, engines by what a NASA CEA run takes. The program office builds what you describe, and its tests return measured data.

## Money and hardware

The program has two budgets.

- **Hardware: $1.6 billion.** It pays for everything on the vehicle, the propellant and the launch. The program office's `cost_report` gives your design's hardware total and each element's cost, rounded, and says exactly whether it is over budget. A design over this budget cannot be built and scores 0.
- **R&D: $240 million.** It pays for tests, which you order from the program office.

What you design is a description. The program office works out masses, strength, aerodynamics and engine performance from it, and what gets built is a little different again. Only tests tell you how it came out:

- **Mass.** Weigh a built element to learn its dry mass.
- **Engines.** A hot fire measures an engine's real Isp and thrust. A new engine design may also hide a flaw, such as combustion instability or a real cooling margin below 1, that destroys it the first time it runs.
- **Reliability.** New hardware fails more often. An untested engine design fails at about 1 to 3% of its starts, and it has a chance of failing in every second of burning. Every hot fire lowers both. A stage's first ignition, a parachute deployment, a separation and an RCS burn can also fail. RCS is for attitude and small burns: its thrust and propellant are capped at the size of flown systems (see `ReactionControlSystem` under "On any element").
- **Hidden reliability.** Those chances are the model's. Each engine you develop, each stage and each parachute design really fails at the model's chance times a hidden factor: 1 on average, but one design in ten is more than twice as failure-prone. Only tests reveal it, through the failures and anomalies they log.
- **Engine failures.** An engine can fail at a start or while burning. These chances do not enter the score (see "How you are scored"); the verifier reports them beside it.
- **Backups and retries.** A parachute failure is survivable with a backup parachute on the same carrier, and a stage ignition failure often with a retry, if every engine on the stage can start again. The verifier flies both cases to find out.
- **Tanks.** Tanks have a real weld and buckling strength. A tank design must keep a factor of safety of 1.6, unless a structural test qualifies it, which allows 1.25.
- **Vibration.** Liftoff noise in the fairing and the shock of each separation can break payload hardware qualified below them. A payload element is anything a fairing covers, or any element that is not a stage. Its `QualificationLevels` set the overall sound pressure level (dB) and the shock level (g) it is built to. Left unset, the acoustic level is the element's predicted liftoff level plus 3 dB, at least 137.4 dB, and the shock level is 1413 g. A clamp band separation gives 1000 g at the plane. A higher level costs structure mass, and a lower one saves it. A vibration test shows whether a unit survives a level. A pump-fed stage whose feed lines resonate with the stack's first longitudinal mode goes into pogo, and the oscillation breaks the stack. A `PogoAccumulator` at the pump inlets detunes the lines; a few tens of litres of gas per engine is typical.
- **Everything else.** Wind tunnel runs measure the drag and lift of your shapes, arc-jet runs the heat load your heat shield absorbs, drop tests your parachutes' drag, and a demo your ISRU plant's output.

You can also buy flight-proven engines, such as Raptor 2, Merlin 1D, RL10B-2 or RS-25. They have no hidden flaws and proven reliability, but they cost what they cost. `program_info` lists them with their published thrust, Isp, mass and price.

## The program office

The board gives you the program tools `program_info`, `program_status`, `design_review`, `cost_report`, `quote_test`, `order_test`, `test_result` and `cancel_test`.

- `design_review` compiles your model from the team repository (`main`, or a branch you name) and tells you whether it could be built: tank walls at proof pressure, engine envelopes, cooling, what fits in the fairing and the aeroshell. It is free and instant. It tells you nothing about masses or performance.
- A test names its target by its part path in your model, e.g. `vehicle.S1.mainEngines`. The program office reads the model when you order, from `main` or the branch you name.
- A test is paid when you order it. It takes real minutes, about 5 to 15, and its result is ready when it ends.
- Each facility runs one test at a time. There are two engine test stands. There is one each of the stage stand, the structures lab, the wind tunnel, the arc-jet, the drop range, the thermal vacuum chamber, the vibration lab and the mass properties lab.
- Only the agent who ordered a test sees its result. Everyone sees the ledger of who ordered what.
- A result is tied to the exact design of the part (its key). If you change the part, the result no longer applies. Freeze a part's design before you pay to test it.

Save every result you get in `program/test_results.json` on `main`, as a JSON list of the result objects, so your teammates see them. The verifier flies the hardware with the program office's own ledger, and checks that every result in the file is one the office gave you, unchanged.

## What to deliver

Commit these to `main` before the run ends:

- `design/vehicle.sysml` (and any other `.sysml` files in `design/`): the whole vehicle and the launch date, in SysML v2 with the domain library. `design-check` must pass.
- `flight/guidance.py`: the flight software. It may import any modules you put in `flight/`. The verifier runs it from a copy of `flight/` alone, so any file it reads must be inside `flight/`.
- `program/test_results.json`: your test results, for the team. They must be the results the program office gave you, unchanged.

Only `main` counts. Files that are not committed are lost.

## How you are scored

The verifier compiles your model, then flies it and your flight software through 48 hidden worlds, drawn at random once and the same for every team. They have different atmospheres, winds, dust storms, hardware variations and navigation errors. Some are harsher than the rest: in about 1 in 10, a dust storm is under way when you reach Mars, and in about 1 in 10, high winds blow over Jezero during the landing. You will never see those seeds. The as-built truth of your parts is the same in every world. Your flight software has to close the loop, not replay a trajectory that worked once.

Each world earns the progress its flight physically made along the mission: the weights of the milestones it reaches, in order, plus partial credit toward the next one for the energy, height, distance or time actually earned. The weights add up to 1. About a quarter of them, 0.24, is landing precision: each landing earns its target's credit by its distance to the target, so a flight that flies the samples home earns close to 1 only if both landings are close to their targets, within a few hundred meters of Jezero and a kilometer or two of the Utah target. The samples wait in a cache at Jezero; a lander more than 10 km from it cannot reach them, and its mission ends at touchdown. A flight that stops short earns what it got: a capsule that burns up at Earth keeps the credit of every milestone before Earth entry.

    task score = G × the mean progress over the 48 worlds

G is the correctness gate: the score is 0 if the design is invalid, the hardware is over budget, or `program/test_results.json` holds a result the program office never gave you or a value you changed.

What counts is what your flights do. Hardware failure chances (see "Reliability") do not weight the credit, and cost counts only through the budgets: the hardware must fit the hardware budget, and the R&D budget limits the tests you can buy. Tests still matter: they reveal what your hardware really is, and your vehicle flies as built.

Every milestone, weight, partial credit, price, test and failure rule is in the sections below.

## Your tools

- The verifier flies 6-DOF: the vehicle as a rigid body, with control authority, aerodynamic moments and stage separation. The manual's "6-DOF mode" section says what can go wrong only there.
- The domain library is in `/opt/designlang/library/`. Every definition in it says what it is.
- `design-check` checks your model: syntax, names, units, designations, how the stack joins and the propellant feeds. It computes nothing about the vehicle.
- There is no mission simulator in your image. The program office flies whole missions for a price (`mission_sim`). Build your own models for sizing, trajectories and the flight software: Python has numpy, scipy, matplotlib, NASA CEA (`import cea`) and `tudatpy`, which includes Lambert targeting, ephemerides, frames and propagation. Correct your models with your test results. There is no internet.
- `examples/` holds a small launcher in SysML and an open-loop ascent script. They show the language and the flight software interface. They get nowhere near Mars.

## Working as a team

You are one of several agents sharing this repo. Your working directory is a clone of the team repo. You push to `main` or to your own branches. You talk through the board tools `send`, `inbox` and `history`.

The mission splits naturally into parts that have to fit together exactly:

- the launcher and Earth ascent
- the trans-Mars trajectory and launch window
- Mars entry, descent and landing
- surface power and thermal
- the Mars ascent vehicle and the return trajectory
- Earth entry and recovery
- systems work: the mass budget, vehicle integration, the end-to-end flight software, and the program's money and test plan

Every part changes the others. A heavier lander needs a bigger aeroshell and a heavier launcher. A different launch date changes everything downstream. Every dollar one part spends is a dollar another part cannot.

Agree early on these points:

- who owns which part
- how changes to the model in `design/` are made, e.g. one file per subsystem
- how the flight software is split into modules
- who decides what the team tests

Keep `main` passing `design-check` and `design_review` at all times, so that nobody's change breaks a part someone else already tested. Keep the team's own models of the vehicle in the repository, next to the measured values they were corrected with.

You have 4 hours. Partial missions score, so get to orbit, then to Mars, then onto the surface, and keep going.

The team's `main` (the design and the flight software, as pushed) is graded at minute 30, hour 1, hour 2, hour 3 and hour 4 of the run, in the same way each time. Your score is the grade at hour 4, when the run ends. The earlier grades are only recorded, to see how the work progressed, and the team does not see them. So keep `main` valid and ready to submit at all times, and push each improvement as soon as it is ready.

## Tools

| Tool | What it does |
|---|---|
| `design-check [FILES]` | Checks the SysML model (default `design/**/*.sysml`): syntax, names, types, units, designations, how the stack joins and the propellant feeds. It lists every problem with its file and line, and the stack from the pad up. It computes nothing about the vehicle. Exits non-zero on any problem. |
| `design_review(ref)` (board) | The program office compiles the model on a branch of the team repository (default `main`) and checks that it could be built: tank walls at proof pressure, engine envelopes, cooling, what fits in the fairing and the aeroshell. It lists every problem, and every part path a test can target. Free and instant. |
| `cost_report(ref)` (board) | The hardware total (to the nearest $0.1M), each element's cost (to the nearest $1M), the room left in the hardware budget, whether the design is over it (exact), and the budgets. Free and instant. |
| `quote_test`, `order_test`, `test_result`, `cancel_test`, `program_info`, `program_status` (board) | R&D tests. See "The program office and R&D tests". |

The program office reads `design/**/*.sysml` from the team repository at the branch or commit you name, so push your model before you call `design_review`, `cost_report` or a test.

## Time and frames

- All times are TDB seconds since J2000.
- Positions and velocities are relative to the current **central body** (`Earth`, `Mars` or `Sun`), in the J2000 inertial frame.
- Around the Sun, the vehicle feels the Sun, every planet and the Moon, at their JPL DE440 positions and masses (Jupiter to Neptune with their moons, at their system centers).
- The central body switches automatically at the sphere of influence:
  - Earth: 924,000 km.
  - Mars: 577,000 km.
  - Entering a sphere happens at 98% of the radius, to avoid chattering at the boundary.
- Latitudes and heights at Earth are geodetic on the WGS84 ellipsoid, as on a map: the sites, the Utah map, `lat`, `lon` and `alt` in the observations, and the touchdown event. `alt` at Earth is the height above the ellipsoid, the height the atmosphere model uses. At Mars, latitudes are areocentric and heights are above the 3389.5 km reference sphere.
- `launchEpoch` in the model sets the start, with the vehicle standing on Kennedy LC-39A (28.6082°N, 80.6041°W, 3 m above the ellipsoid). It must fall between 2030-01-01 and 2035-12-31. The mission must end before 2041-10-01. Planet positions come from JPL DE440; Mars's gravity is JGMRO_120F to degree 30 and the Jezero ground the USGS HiRISE maps at 20 m. Above the atmosphere interface, up to 1000 km, a thin drag still lowers orbits around Earth and Mars.

## The design: `design/vehicle.sysml`

The vehicle is a SysML v2 model in the textual notation (OMG SysML v2.0, https://www.omg.org/spec/SysML/2.0). It uses the domain library in `/opt/designlang/library/`. Read the library: every definition has a `doc` comment. `examples/ascent_vehicle.sysml` is a complete model of a small launcher, and a good place to start.

A model is a package that imports the library and declares one part typed by `MissionStack`:

```
package MarsSampleReturn {
    private import ScalarValues::*;
    private import SI::*;
    private import MarsReturnUnits::*;
    private import MarsReturnLibrary::*;

    part def R2 :> ProcuredEngine { :>> designation = "Raptor 2"; }

    part vehicle : MissionStack {
        :>> launchEpoch = "2030-12-28T00:00:00";
        part S1 : LaunchStage {
            :>> outerDiameter = 11.2 [m];
            :>> propellantLoad = 1150000 [kg];
            part fuelTank : PropellantTank { ... }
            part oxidizerTank : PropellantTank { ... }
            part pressurization : PressurizationSystem { :>> meop = 3.0 [bar]; }
            part mainEngines : R2[9];
            connection : PropellantFeed connect fuelTank to mainEngines;
            connection : PropellantFeed connect oxidizerTank to mainEngines;
        }
        part S2 : LaunchStage { ... }
        connection : StageJoint connect S1.fwd to S2.aft;
    }
}
```

How a model is written:

- Every value carries its unit in brackets, e.g. `11.2 [m]`, `2.85 [mm]`, `3.0 [bar]`, `70 [deg]`, `5.0 [deg/s]`, `0.05 [W/(m^2*K)]`. The checker rejects a value whose unit does not fit its quantity. Ratios, counts and load factors have no unit.
- `:>> name = value;` sets an attribute the library declares. A misspelled name is an error.
- A count in brackets after a type gives that many identical parts, e.g. `part mainEngines : R2[9];`.
- The names of the stack elements (`S1`, `HS`, `CAP` and so on) are the ids your flight software uses for them. Tests, failure chances and the as-built hardware are tracked by these ids too, so each element needs a name of its own. Two elements with one name make the design invalid.
- You may split the model over several files in `design/`. They are read together.

### The stack

`StageJoint` connections list the launch stack from the pad up. Each one joins the lower element's `fwd` port to the upper element's `aft` port. Every element needs a joint above and below it, except the top and the bottom.

- A `PayloadFairing` covers every element above it, and it must be wide enough (0.2 m of clearance) and long enough for them.
- A `Heatshield` and the next `Backshell` above it form an aeroshell. The elements between them ride inside it and must fit within 92% of the heatshield's diameter. The backshell's size follows from what it encloses.
- A `Parachute` rides on the nearest backshell or capsule below it. Separating a backshell takes its own parachutes along.
- `{ :>> interstage = true; }` on a joint puts an interstage skirt under the upper element. It stays with the lower element when they separate, as on Falcon 9. The default is no interstage.

**Sizes the program builds.** Past these sizes nothing has flown or been tested, and `design_review` refuses the design:
- stages and fairings at most 12 m across (Saturn V's stages were 10.1 m, Starship's are 9 m);
- rigid heat shields and capsules at most 12 m across (MSL's aeroshell was 4.5 m, Orion's heat shield is 5 m);
- at most one supersonic (disk gap band) parachute on the vehicle: Viking, MSL and Mars 2020 each flew one, and no cluster of supersonic canopies has flown;
- at most 33 engines on one stage (Super Heavy).

### Stages

`LaunchStage`, `Lander` and `AscentStage` are stages. A `SurfacePlatform` is a stage without propulsion. A stage has:

- `outerDiameter`.
- `propellantLoad`: the propellant loaded at launch. It is split between the tanks at the engines' mixture ratio. `oxidizerLoadFraction` below 1 launches the oxidizer tank part empty, e.g. to fill it with ISRU oxygen on Mars.
- One fuel `PropellantTank` and one oxidizer `PropellantTank`, each with:
  - `propellant`, by name and, if you like, its specification, e.g. `"LOX (MIL-PRF-25508)"`;
  - `material`, by its handbook designation (see "Materials"), and a `layup` for a composite;
  - `radius` (inner), `barrelLength` and `domeAspectRatio` (dome height over radius, 0.7071 only for now);
  - `wallThickness`, the skin gauge;
  - `ullageFraction` (default 0.03).
  A tank with a barrel has the stage's radius. A tank of two domes only (`barrelLength = 0 [m]`) may be narrower. The tanks hold their volume less the ullage. A tank drawn bigger than the mixture ratio needs is allowed, and it carries the weight of its unused part. Both tanks of a stage share one material, layup and ullage.
- A `PressurizationSystem` with `meop`, the maximum expected operating pressure of the tanks. Pressure-fed engines need a MEOP of 1.25 times their chamber pressure plus 2 bar. Pump-fed engines need 1.8 bar.
- Engines, with a `PropellantFeed` from each tank to each engine part. All engines on a stage share its propellants and mixture ratio.
- Optional parts:
  - `MultiLayerInsulation` (`layers`) on every tank, for long cryogenic storage. Fewer than 10 layers insulates poorly.
  - `SprayOnFoamInsulation` on the cryogenic tanks of a launch stage.
  - `SurfaceThermalBlanket` (`heatLeakCoefficient`): the heat leak of the tanks on the Mars surface, in W/(m²·K). Without one it is 0.5.
  - `LandingGear` (`touchdownSpeedRating`): legs rated for a touchdown speed, 0.5 to 6 m/s.
  - `TerrainRelativeNavigation` on a lander: a descent camera and vision computer that fix the position near the ground ("Terrain and landing" below).
  - `CommonBulkhead`: one dome separates the two propellants inside a single tank body, as on the S-II, S-IVB, Centaur and Falcon 9. It replaces the intertank and makes the stage shorter. Between LH2 and the other propellant the dome is an insulating sandwich. With a common bulkhead, both tanks have the same radius, and the tank body is one barrel (the two tanks' `barrelLength` added) closed by two outer domes. The bulkhead sits where the mixture ratio puts it.
  - `StringerRingStiffening` in each tank (the same in both): stringers along the axis and rings around it, inside the tank barrels, as on the S-IC (T-section stringers) and the Shuttle External Tank (an orthogrid of blades). `wallThickness` is then the skin. Its attributes are `stringerHeight`, `stringerThickness`, `stringerSpacing` (around the tank) and `ringSpacing` (along it). Optional: `stringerFlangeWidth`, a flange that wide and as thick as the stringer on its top edge, which makes a T (the height includes the flange); `ringHeight` and `ringThickness`, which default to the stringer's. Buckling uses the stiffened cylinder method of NASA SP-8007 (section 4.3.1), for the whole wall and for one bay between two rings, times SP-8007's factor of 0.75. The skin between stiffeners and the stiffeners themselves must not buckle as flat plates, and the skin and stringers must not yield. Tank pressure gets no stabilization credit on a stiffened wall. Stiffeners cannot go on balloon tanks.
  - `RingBaffles` in each tank (the same in both): flat rings against slosh, `widthFraction` of the tank radius (0.05 to 0.3), every `spacing`.
- `PressurizationSystem.balloonTanks = true` makes pressure-stabilized tanks, as on Atlas and Centaur, in `AISI 301 full hard` only. The walls may be as thin as 0.35 mm, but the skin takes no compression. In flight the tank pressure alone must carry every axial and bending load, with the factor of safety. Other walls are at least 0.8 mm.
- Every LH2 tank carries insulation, because air would liquefy on a bare one. The program office adds it.

### Engines

Buy a flight-proven engine by its designation: `part def R2 :> ProcuredEngine { :>> designation = "Raptor 2"; }`. The catalog has Merlin 1D, Merlin 1D Vacuum, Raptor 2, Raptor Vacuum, RS-25, RL10B-2 and AJ10-190. `program_info` lists each one's published vacuum thrust, vacuum Isp, mass and price. A catalog engine performs as published, has no hidden flaws, and has flight-proven reliability.

Or develop your own, a `LiquidRocketEngine`, described the way a NASA CEA run and a drawing describe it:

| Attribute | Values |
|---|---|
| `fuel`, `oxidizer` | `LH2`/`LOX`, `LCH4`/`LOX`, `RP-1`/`LOX`, `MMH`/`NTO` |
| `cycle` | `EngineCycle::pressureFed` (up to 25 bar), `electricPump` (up to 60), `gasGenerator` (up to 120), `expander` (LH2: up to 110 bar and 300 kN; LCH4: up to 60 bar and 150 kN), `fuelRichStagedCombustion` (up to 230, not RP-1), `oxidizerRichStagedCombustion` (up to 270, RP-1 or LCH4 only), `fullFlowStagedCombustion` (up to 350, LCH4 or LH2) |
| `chamberPressure`, `mixtureRatio`, `areaRatio`, `throatDiameter` | Chamber pressure, O/F by mass, nozzle area ratio (2 to 300), throat diameter |
| `cooling` | `ChamberCooling::regenerative`: the fuel must absorb the wall heat. `ablative`: up to 30 bar and 900 s of total burn. `radiative`: up to 25 kN and 15 bar. |
| `chamberMaterial`, `nozzleMaterial` | A regenerative chamber is a copper alloy or a superalloy (`NARloy-Z`, `GRCop-42`, `GRCop-84`, `OFHC copper`, `Inconel 718`, `AISI 347`). An ablative chamber has a `silica phenolic` or `carbon phenolic` liner. A radiative chamber is `C-103`, `Re/Ir` or `Pt-Rh`. Nozzle extensions: `Inconel 718`, `AISI 347`, `C-103`, `carbon-carbon`, `Ti-6Al-4V annealed`, or the chamber materials. |
| `filmCoolingFraction` | 0 to 0.15. It lowers the wall heat load and costs Isp. Ablative and radiative chambers need at least 0.10. |
| `injector` | `InjectorType::conventional`, or `pintle` (deeper throttling, heavier, less prone to combustion instability) |
| `ignition` | `IgnitionSystem::hypergolic` (NTO only), `sparkTorch` (cryogenic pairs, unlimited starts), `teaTeb` (one start per cartridge; set `igniterUnits`) |
| `gimbalRange` | Thrust vector control. It is needed for steering, together with or instead of RCS. |

The ideal performance of your engine is what CEA gives for its propellants, chamber pressure, mixture ratio and area ratio. A real engine falls short of that: injector efficiency, nozzle divergence, boundary layer and kinetic losses, turbine exhaust and film cooling all cost Isp. How much is for your hot fires to measure. Gas generators dump their turbine exhaust, and their power balance must close. Electric pumps draw battery power, so their stage needs a `Battery`. Thrust at altitude is the vacuum thrust less the ambient pressure times the nozzle exit area. A nozzle whose exit pressure is below about 0.15 of the ambient pressure separates, and the side loads break it.

### Entry systems

- `Heatshield`: `baseDiameter`, `halfAngle` (45 to 70 deg), `noseRadius`, `trimAngleOfAttack`, and a `ThermalProtection` part with its `material` and `thickness`. A trim angle comes from a radial offset of the center of mass, made with entry ballast that `jettison_ballast` drops. It gives the aeroshell lift, which the flight software steers by bank angle. A 70 deg sphere-cone trims to at most about 20 deg.
- `Backshell`: a `ThermalProtection` part, and often a `ReactionControlSystem`.
- `Parachute`: `canopy` (`CanopyType::diskGapBand`, Mach up to 2.2 and q up to 850 Pa; `ringsail`, Mach up to 0.8 and q up to 4,500 Pa), `nominalDiameter` and `designDynamicPressure`. A canopy opens over a short time, and it must hold the peak load of its opening:
  - After `deploy`, the lines take 0.053 s per meter of diameter to stretch, e.g., 1.14 s for a 21.5 m canopy. Until then the canopy has no drag. (MSL's 21.35 m parachute took 1.135 s on Mars, Cruz et al. 2013.)
  - The canopy then fills while it travels 12.5 diameters, e.g., in 0.67 s for a 21.5 m canopy at 400 m/s. This is the filling distance of Knacke's Parachute Recovery Systems Design Manual, and MSL and the three ASPIRE tests filled in 11.2 to 13.5 diameters. The drag grows over the filling time.
  - A `ringsail` is reefed, as every large Earth landing canopy is: it opens in Apollo's stages, first to 6.8% of its full drag area, then 6 s after line stretch to 25.7%, then 10 s after line stretch to full open (Apollo main parachutes, NASA TN D-7437). Each stage fills over 12.5 diameters times the square root of the drag area it adds, as a share of the full drag area (Knacke). A `diskGapBand` is not reefed.
  - At full inflation of each stage the canopy reaches its peak drag area, 2 × kP × S_proj (Way 2018), times the stage's share of the full drag area. The opening load is the largest drag on the canopy while it fills: F = 2 × kP × q × S_proj at full inflation when the vehicle is heavy and fast and hardly slows meanwhile, as on Mars, and less, earlier in the filling, when a large canopy slows a light vehicle down while it fills. S_proj is the inflated canopy's projected area, 0.539 (`dgb`) or 0.578 (`ringsail`) of the canopy's area π × diameter² / 4. kP is 0.752 on average, and it changes from one opening to the next with a spread of 0.080 (one sigma, from ten flown openings). The parachute's real drag factor (see "The hidden truth of the hardware") and its flight-to-flight spread scale this load as they scale the drag. For MSL's opening (474 Pa) the model gives 139.5 kN, against the 153.8 kN measured. For the ASPIRE tests the model is within 2.2%.
  - After full inflation the canopy has its steady drag (for a reefed stage, its share of it).
  - Canopies of the same type and size open as a cluster, and a cluster drags less than its canopies would alone: 0.97 of the sum for two and 0.945 for three, Apollo's measured values (Knacke), and 0.0275 less for each canopy beyond three.
  - The opening shock, the peak loads of the cluster's canopies divided by the vehicle's mass, counts against the capsule's `maxLoadFactor`.
  - A canopy holds the peak load of a hard opening (kP = 0.938) at its `designDynamicPressure`, in its first stage if it is reefed, with Apollo's 8% margin for the stages after it. A larger peak load in any stage tears it. With an average kP, the dynamic pressure at full inflation may then be up to 1.25 × `designDynamicPressure`. In a hard opening it may be up to `designDynamicPressure`.
- `EarthEntryCapsule`: the Earth return capsule. Exactly one is required, and the samples go in it. It has `baseDiameter`, `halfAngle`, `noseRadius`, `trimAngleOfAttack`, `landing` (`EarthLanding::parachute`, up to 10 m/s at touchdown, or `crushable`, up to 45 m/s), a `ThermalProtection` part, a `SampleContainmentAssembly` with its quoted `mass` and `sampleContainers` (the Orbiting Sample containers it brings home: 1 if you give none, up to 20; each holds 30 tubes and weighs about 11 kg loaded, and each beyond the first adds 0.5 kg of containment per kg and its own $10M container and sealing), and a `DecelerationLimit` requirement with `maxLoadFactor` (5 to 150, in Earth g; 60 if you give none).

### On any element

- `AttitudeControlSystem` (`maxSlewRate`, 0.5 to 30 deg/s; 5 deg/s without one).
- `ReactionControlSystem`: hydrazine thrusters with `propellantLoad` and `totalThrust`, Isp 225 s, for small burns and ullage settling.
  RCS is sized like flown systems, not like a main engine. `totalThrust` is at most 147,000 N per element, the 38 primary thrusters of 3.87 kN on the Space Shuttle orbiter (NTRS 19910018886). `propellantLoad` is at most 25% of the element's own mass without it (dry mass plus main propellant), or 50 kg if that is more. That is about 490 m/s on the element alone. The Apollo lunar module tanked about 280 kg of RCS propellant on a 4.7 t ascent stage, 6% (Apollo 11 Mission Report Supplement 5, NTRS 19720018196). RCS burns count toward reliability (see "Reliability").
- Power: `MMRTG` parts (110 W each, at most 3 per element), `FissionPowerUnit` parts (10 kWe each), `SolarArray` (`area`), `Battery` (`energy`, e.g. `50 [kW*h]`).
- `Cryocooler` (`inputPower`) and `IsruOxygenPlant` (`inputPower`; it makes oxygen on Mars, about 0.75 kg per sol per kW as designed).
- `EquipmentItem` (`mass`): equipment the library does not model, at its quoted mass.
- `TelecomSystem` (`band` X or Ka, `highGainAntennaDiameter`, `transmitPower`, `gimbal`, `uhfRelay`): the element's radio. Without one an element keeps a 15 W X-band amplifier and low-gain antennas. A bigger dish, amplifier, gimbal or UHF radio adds mass and transmit power.
- `Redundancy` (`avionics` single, dual or crossStrapped; `rcsBranches`, `landingRadars`, `pyroInitiators`, `isruUnits` 1 or 2; `batteryStrings` 1 to 4): spare hardware against part failures in flight, each at its own mass.
- On a stage, `PropellantGauging` (`gauge` capacitance, pointSensors or pvt; `lowLevelCutoff`): how the stage reads its tanks and stops its engines when one runs low.
- On the capsule's `SampleContainmentAssembly`, `sampleCarrierAlphaOverEpsilon` (default 0.5): the carrier finish, which sets the samples' temperature in sunlight.
- On the `MissionStack`, `dsnPassesPerDay` (1 to 3, default 3) books the Deep Space Network, and `groundWindLimit` (default 17.5 m/s) is the highest ground wind the vehicle launches in.

### Materials

Tank materials are named by their handbook designation:

| Designation | Handbook |
|---|---|
| `AISI 301 full hard` | MMPDS |
| `2219-T87` | MMPDS |
| `2195-T84` | MMPDS |
| `Ti-6Al-4V annealed` (not with LOX: impact ignition) | MMPDS |
| `IM7/8552`, `AS4/3501-6`, with a layup such as `"[45/-45/0/90]2s"` (a liner for LOX and LH2 is included) | CMH-17 |

TPS materials: `PICA`, `SLA-561V`, `AVCOAT 5026-39` and `carbon phenolic`. Each has a heat flux it is rated for, and a layer of it absorbs a heat load that grows with its thickness. The arc-jet measures both for your layer.

The L/D a trim angle gives is its hypersonic value. With a fixed CG offset, the trim angle of attack grows as the capsule slows, so L/D grows too. The table gives L/D as a ratio to that hypersonic value, for every heatshield and capsule. It is MSL's preflight aerodynamic database, and the flights of MSL and Mars 2020 followed it within 0.01.

| Mach | 24 and above | 20 | 15 | 10 | 5 | 4 | 3 | 2.5 | 2 and below |
|---|---|---|---|---|---|---|---|---|---|
| L/D ÷ hypersonic L/D | 1.000 | 1.012 | 1.058 | 1.107 | 1.218 | 1.263 | 1.317 | 1.325 | 1.400 |

Propellants: `LOX` (MIL-PRF-25508), `LH2` (MIL-PRF-27201), `LCH4`, `RP-1` (MIL-DTL-25576), `MMH` (MIL-PRF-27404), `NTO` (MIL-PRF-26539), and `N2H4` (MIL-PRF-26536) for RCS.

The program office builds your parts from these materials. Their density and strength as built are the program office's. Handbook values are a fair first estimate. Built hardware differs from the handbook, and tests measure it.

### What the program office checks

`design_review` and the verifier reject a design that could not be built:

- Each tank wall must hold the MEOP times the factor of safety at yield. The factor is 1.6 for a tank design that was never tested (the no-test factor of NASA-STD-5001) and 1.25 for one that passed a structural qualification test.
- In flight, the tanks must also survive axial and bending loads with the same factor. Buckling uses the NASA SP-8007 knockdown, and tank pressure helps: it carries part of the axial load and stiffens the shell (SP-8007, eq. 34 and Figure 6).
- Under acceleration the propellant presses on the lower wall of its tank, on top of the tank pressure. The program office sizes each tank for the highest acceleration it sees while full, at the burnout of a stage below it or at its own ignition. Where your wall cannot hold the tank pressure plus that head with the factor of safety, it thickens the lower wall, and the stage gets heavier. Your walls must still hold the MEOP alone.
- Separate tanks are joined by an intertank skirt, which the program office sizes for the axial load above it.
- Engines must stay inside their cycle's envelope, close their power balance and cool their chamber.
- The fairing and the aeroshell must fit what they enclose.
- Each `ReactionControlSystem` must stay within the RCS limits under "On any element".
- The vehicle needs a stage with engines and exactly one Earth return capsule.

Masses, centers of mass, tank and engine performance and costs are never declared. The program office works them out from what you describe.

A `TerrainRelativeNavigation` part on a stage adds terrain-relative navigation, a descent camera and a vision computer like the Lander Vision System of Mars 2020. It weighs 15 kg and costs $10M. It works once no heatshield is attached. The first time the flight software is called between 4,200 m and 500 m above the ground, the camera matches the ground below to the onboard map. The navigated position error then drops to 13 m per axis, and the event `trn_fix` is reported. The velocity error does not change. Without it, the position error from entry interface stays all the way down, about 1 km. "Terrain and landing" below says how to use the maps.


## Costs

The program office prices every part of the design with the price list below. The prices are engineering estimates for a modern commercial program, calibrated against public figures for flown hardware. Treat them as the rules of this program. `cost_report` gives the total, rounded, and each element's cost, rounded, but not the bill part by part: several prices are per kg of the built part, and the built masses are for your models and your `weighing` tests to find.

**Budgets:** hardware $1.6 billion, R&D $240 million. A design whose hardware costs more than the hardware budget cannot be built, and the verifier scores it 0.

**Engines you develop.** The first unit costs

    $0.4M + K × (F_vac / 1 MN)^0.6 × (pc / 100 bar)^0.25 × propellant factor

times 0.6 for ablative cooling, 0.7 for radiative, and 1.1 for a pintle injector.
- K by cycle: pressure_fed $1.6M, electric_pump $2.0M, gas_generator $3.0M, expander $15M, staged_ox $7M, staged_fuel $14M, full_flow $6.5M.
- The propellant factor is LH2 1.5, LCH4 1.0, RP1 0.9 and MMH 1.2.
- Every unit of one engine design across the vehicle follows an 85% learning curve: the n-th unit costs n^log2(0.85) times the first.

For scale, a Merlin-like design comes to about $3M for the first unit, a Raptor-like design about $15M, an RS-25-like design about $42M and an RL10-like design about $5M.

**Catalog engines,** per unit with no learning: Merlin 1D $1.5M, Merlin 1D Vacuum $2.5M, Raptor 2 $2.0M, Raptor Vacuum $4.0M, RS-25 $100M, RL10B-2 $15M, AJ10-190 $6M.

| Part | Price |
|---|---|
| Tanks, per kg of tank mass, common bulkhead, propellant head walls and stiffeners included | AISI 301 $150, 2219 $900, 2195 $1,600, Ti-6Al-4V $2,500, carbon/epoxy $3,000 |
| Each stage | $4M integration, $0.15M per engine installed |
| Thrust structure, interstage, pressurant system, secondary structure and stage systems (intertank included) | $1,000, $1,500, $800 and $1,200 per kg |
| Foam or LH2 insulation, MLI | $300 per m²; $500 per m² plus $40 per m² per layer |
| Fairing | $25,000 per m² of shell |
| Avionics | $3M per element that carries them |
| Heat shield TPS, per m² of forebody at 10 kg/m² (times (areal mass / 10)^0.5) | SLA-561V $60k, PICA $200k, AVCOAT $250k, carbon phenolic $350k |
| Aeroshell and backshell structure | $8,000 per kg |
| Capsule | $30,000 per kg of structure and containment, plus $10M for the sample container, sealing and recovery aids, and $10M more for each further sample container |
| Parachute | $1M + $10,000 × diameter² × (design q / 500 Pa)^0.5 |
| Landing legs | $8,000 per kg |
| Landing system, once for each element with legs (landing radar, telecom, thermal control, and assembly and test as a spacecraft; terrain-relative navigation is its own part, below) | $40M |
| RCS | $1.5M + $50 per N + $200 per kg of propellant capacity; hydrazine $250 per kg |
| Power | solar arrays $250,000 per m², batteries $15,000 per kWh, RTGs $40M each plus $95M once for a vehicle that carries any (fueling and nuclear launch approval), fission units $250M each |
| Cryocoolers, ISRU plant | $4M and $25M per kW |
| Extra equipment | $5,000 per kg |
| Terrain-relative navigation (`TerrainRelativeNavigation`) | $10M |
| Surface insulation beyond the default | $3,000 per kg |
| Propellant, per kg | LOX $0.25, LCH4 $0.80, RP1 $2, LH2 $8, MMH $200, NTO $80 |
| Launch operations | $20M |

## The program office and R&D tests

The board has these program tools:
- `program_info`: test kinds, facilities, prices, the flight-proven engines, rules and status
- `program_status`: budget committed and left, queues, your tests and the ledger
- `design_review(ref)` and `cost_report(ref)`: see "Tools"
- `quote_test(kind, target, params, ref)`: the price, duration and part key, without ordering
- `order_test(kind, target, params, note, ref)`: pays from the R&D budget and queues the test
- `test_result(test_id)`
- `share_result(test_id)`: adds a done test's result, exactly as the office gave it, to the team's shared record (see "Saving results")
- `cancel_test(test_id)`: for queued tests, with a full refund

A test's `target` is a part path in your model, e.g. `"vehicle.S1.mainEngines"` for the engines of S1. `design_review` lists every target. `ref` is the branch or commit of the team repository that the program office reads, default `main`. It reads the model when you order, and the test measures that design, even if the model changes before the test runs.

The rules:
- A test is paid when ordered. The office bills in steps of $0.1M, rounding the price below up. An order that would overspend the R&D budget is refused.
- Each facility runs one test at a time, first come first served. There are two engine stands, and one each of the stage stand, structures lab, wind tunnel, arc-jet, drop range, thermal vacuum chamber and mass properties lab.
- A test takes the real minutes listed below. Its result is ready when it ends.
- Only the agent who ordered a test sees its result. The ledger (who ordered what, the part key, the cost and the status) is public.
- A result applies to the exact part design with that key. Change the part, and it is a new design with its own unknown truth and no test history. Keys round the values that define the part. For engines, those are chamber pressure to 1 bar, mixture ratio to 0.01, area ratio to 0.5 and throat to 1 mm. Gimbal range is not part of the key.
- Results are measurements of the built article: absolute values, with the scatter of real hardware and real instruments.

| Test | Target | Price | Minutes | What it measures |
|---|---|---|---|---|
| `engine_hotfire` | an engine part; params `starts` 1 to 12, `burn_s` 1 to 900 | 60% of the engine's first-unit cost (the test article) + $0.2M + $0.25M × (F_vac / 1 MN)^0.5 per start + propellant | 3 + 0.4 per start + total burn seconds / 300 | Vacuum Isp and vacuum thrust; the cooling margin (once the test fires over 20 s in all); whether the design is unstable; random failures and anomalies at the design's real reliability. Every start and second counts toward the design's reliability. |
| `stage_static_fire` | a stage with engines; params `burn_s` 5 to 300 | 12% of the stage's cost + $1M + $0.50 per kg of propellant burned | 12 | Integration problems, with failures and anomalies at the stage's real reliability. The stage's first-ignition failure chance in the model drops from 2% to 0.4% after one, and to 0.2% after two. It also counts as a start and a burn for every engine on the stage. |
| `structural_qualification` | a stage with tanks | 50% of the tank cost + $1.5M | 8 | A proof test at 1.25 × MEOP and an axial compression test of a tank article: pass, or the pressure the tank burst at; the weld efficiency; and the buckling load over the SP-8007 design value. A pass qualifies this tank design for the 1.25 factor of safety. The tank's key includes its size (its capacity, to 1,000 kg), so a pass covers tanks of the size tested, and the result says so. A tank with shorter barrels, on main or on another branch, costs less to test but does not qualify a longer one. |
| `wind_tunnel` | `"vehicle"` for the launch stack, or a heatshield or capsule | $2.5M for a stack, $3.5M for a shell | 6 | The stack's axial force coefficient, or the shell's drag coefficient, from Mach 0.6 to 10, and the shell's lift-to-drag at trim, to about 1%. |
| `arcjet` | an element with a ThermalProtection part; params `heat_flux_W_m2`, `duration_s` | $1.2M + $2,000 per second | 5 | Whether the TPS material survives that heat flux, and the heat load in J/cm² that your layer absorbs before its bond line gets too hot, to about 3%. |
| `parachute_drop` | a parachute | dgb $9M (a supersonic rocket-boosted test), ringsail $2M | dgb 15, ringsail 8 | One deployment at the parachute's real reliability: success or failure, anomalies, and the drag coefficient. Each drop multiplies the model's deployment failure chance by 0.65. |
| `isru_demo` | the element with the IsruOxygenPlant | $6M | 10 | The plant's oxygen output in kg per sol at its rated power. |
| `weighing` (the old name `mass_properties` still works) | one element of a whole design | 25% of the element's cost (a flight-like article is built to weigh) + $0.25M | 6 | The element's as-built dry mass, weighed on a scale accurate to 0.1%. |
| `mission_sim` | `"vehicle"`: the whole design and `flight/` at the `ref`; params `world` (`"random"`, `"nominal"` or seeds) and `worlds` 1 to 4 | $1M per world | the flights' real time, up to 25 per world | Each world flown end to end, as the verifier flies it (see "Mission simulation"). |

More test kinds come with the realism models: `vibration_test`, `weighing` (in the table above; it runs on the stage stand), and the failure tests `avionics_burn_in`, `phasing_test`, `trajectory_requalification`, `part_lot_screening`, `centrifuge_test`, `edl_hil_test`, `nav_end_to_end`, `radiation_test` and `touchdown_sensor_test`, in a vibration lab and an avionics lab. The sections from "The realism that is active" on describe what each one reveals. `program_info` always lists the kinds, prices and facilities that are open now.

**Mission simulation.** `mission_sim` flies your committed design and `flight/guidance.py` through whole worlds on the program office's simulator: 6-DOF, the same models and limits as the verifier (25 minutes of wall-clock time and 20,000 calls per world).
- **Which worlds, your choice (`params.world`):**
  - `"random"` (the default) flies `worlds` fresh dispersed worlds, 1 to 4, drawn like the verifier's but never the verifier's own.
  - `"nominal"` flies the one world with no dispersions: the same every time, the place to debug.
  - A seed, or a list of up to 4 seeds, from an earlier result flies those worlds again, so you can check a fix against the world that failed.
- **It flies the design as specified, not the as-built hardware.** Your other tests measure what was built; the simulation uses the design's own values, with the tank qualifications you have earned and the failure tests you have done (a latent flaw a test has made unlikely is as unlikely in the simulation as in the verifier).
- **What you get, per world:** its seed (none for the nominal world), the milestones reached and their mission times, the failure that ended the flight and when, peak dynamic pressure, load factor and heating, landing errors, the number of guidance calls and the wall time, and the flight's events (up to 200). Nothing your flight software writes or chooses at run time comes back: for an exception you get only its built-in type, not its message or the file and line where it was raised; for a malformed command, which field was wrong, not its value. Test your flight software locally to read its own output.
- **Ordering:** it reads `design/` and `flight/` at the commit `ref` points to when you order. Two simulations run at once, on the `mission_simulator` facility.
- **Cost:** $1M per world, paid from the R&D budget like any test.

**Saving results.** `share_result(test_id)` shares a result with the whole team: the office adds it, exactly as it gave it, to `program/test_results.json` on the team repository's `results` branch, which only the office writes. Read it with `git fetch origin results && git show origin/results:program/test_results.json`. Only the agent who may read a result can share it. You may also keep result objects in `program/test_results.json` on `main` yourselves; there every number of an entry is checked against what the office gave (see "How you are scored"), so copy them whole or not at all. The verifier uses the program office's own ledger, not these files. The flight software cannot read it in the verifier either: copy any measured values it needs into `flight/`.

## The hidden truth of the hardware

What gets built differs from the program office's design model. The truth of each part is drawn from a secret held by the program office, from its key, so it stays fixed for a design during the whole run and is the same in all 48 verifier flights. The distributions are public:

| Part | What varies |
|---|---|
| Engine you develop | Isp factor normal(0.994, 0.006), from 0.97 to 1.01. Thrust factor normal(1.0, 0.01), from 0.97 to 1.03. Combustion instability with a chance of 4% + 8% × (pc / cycle maximum)², plus 6% for a throat over 0.3 m, less 4% for a pintle injector (at least 1%). An unstable engine is destroyed at its first ignition. The real cooling margin of a regen engine is the design margin times normal(0.93, 0.07). Below 1, the chamber burns through once the engine has fired over 20 s, counting every burn. |
| Catalog engine | Nothing varies. |
| Tank design | The same for any capacity with the same walls (material, diameter, MEOP, propellants, wall thicknesses and construction), so a test of a short tank measures them for a long one too. Weld efficiency, uniform per material: AISI 301 0.85 to 0.98, 2219 0.75 to 0.92, 2195 0.70 to 0.90, Ti-6Al-4V 0.85 to 0.95, carbon/epoxy 0.80 to 0.95. A tank whose hoop stress at flight pressure exceeds yield times weld efficiency bursts at launch. The buckling strength factor against SP-8007 is normal(1.05, 0.08), from 0.85 to 1.25. |
| Launch stack | Drag factor normal(1.0, 0.06), from 0.85 to 1.2. |
| Heat shield or capsule shape | Drag factor normal(1.0, 0.04), from 0.9 to 1.1. Lift-to-drag factor normal(1.0, 0.08), from 0.8 to 1.2. |
| TPS material | Heat-load capacity factor normal(0.93, 0.08), from 0.75 to 1.1. The heat flux it survives is the rating times normal(0.95, 0.08), from 0.75 to 1.15. |
| Parachute design | Drag factor normal(0.97, 0.04), from 0.88 to 1.06. |
| ISRU plant | Oxygen output factor normal(0.85, 0.12), from 0.5 to 1.1. |
| Reliability of an engine you develop, a stage or a parachute design | A factor on all its failure chances, gamma with shape 2 and mean 1. See "Reliability". |

With the 1.6 factor of safety, an untested tank never bursts, because every weld efficiency is above 0.625. A qualified tank uses 1.25, so its welds must hold at least 80% of yield. The qualification test finds out whether they do.

## Reliability

**Reported, not scored.** The task score is what your flights physically do (see "How you are scored"). From the chances below the verifier also computes how likely your hardware was to survive each flight, and reports it beside the score; it does not change the score.

Every piece of hardware can fail. The model gives each part these failure chances, and testing buys them down (the Duane reliability growth model):

- **Engine start:** p_start = P0 × (1 + 0.5 × pc / cycle maximum) / (1 + test starts / 3)^0.7.
- **Engine burning:** a failure rate per second of λ = Λ0 × (1 + 0.5 × pc / cycle maximum) / (1 + test seconds / 100)^0.7, for each engine that is burning.

| Cycle | P0 | Λ0 (per s) |
|---|---|---|
| pressure_fed | 0.4% | 0.8e-5 |
| electric_pump | 0.8% | 1.5e-5 |
| gas_generator | 1.0% | 2.0e-5 |
| expander | 1.0% | 2.0e-5 |
| staged_ox, staged_fuel | 1.8% | 3.5e-5 |
| full_flow | 2.0% | 4.0e-5 |

- **Catalog engines:** 0.15% per start and 3e-6 per second.
- **RCS burns:** 2e-6 per kN-second of RCS impulse, for every `rcs` command, every `settle`, and the attitude control that the onboard controller flies with the RCS. RCS thrusters are flight-proven: they have no hidden factor and no test growth. A 150 kg RCS burned dry is about 330 kN-s, a 0.07% failure chance.
- **A stage's first ignition in flight:** 2% untested, 0.4% after one static fire, and 0.2% after two.
- **Parachute deployment:** dgb 8% and ringsail 4% untested, times 0.65 per drop test, down to 0.3%.
- **Each separation:** about 0.18%, flown in the flight itself: a separation that fails leaves the element attached.

Test starts and seconds count for the exact engine design, from hot fires and from static fires of stages that carry it. More engines and more starts mean more chances to fail, and the survival the verifier reports counts every engine failure as lost (see "When an engine fails").

### The real chances are hidden

The chances above are the model. A new design really fails at the model's chances times a hidden reliability factor. Every engine you develop, every stage with engines and every parachute design has its own factor. It is drawn from the program office's secret and the part's key, like the other hidden truths, so it stays fixed for a design during the run and in all 48 verifier flights. Change the design and you get a new factor.

The factor follows a gamma distribution with shape 2 and mean 1. About one design in four is less than half as failure-prone as the model, and about one in ten is more than twice as failure-prone. Flight-proven catalog engines have no hidden factor, and separations always fail at 0.2%.

Testing lowers the real chances by the same growth as the model's. A design with a factor of 2 stays twice as failure-prone as the model at every point of its testing.

### How tests reveal the factor

Hot fires, static fires and drop tests run at the real chances. Besides failures, they log anomalies: off-nominal events that did not end the test, such as a hard start that damaged nothing, a pressure spike, or a seam that tore without losing the canopy. Anomalies show the design's margins. They come at 20 times the design's untested failure chance, times its factor, at every start, second of burning, stage ignition and deployment. Testing fixes failure modes, so failures get rarer, but the anomaly rate stays the same however much a design is tested, so every test adds evidence.

Each of these results has `reliability_evidence`:
- `key`: the part design it is about. A hot fire informs its engine design, a static fire its stage, and a drop test its parachute. A static fire still counts as starts and seconds of growth for the engines on the stage.
- `failures` and `anomalies`, and `events`, which is their sum.
- `expected_events`: the events the model would expect over the same engine starts and seconds, stage ignition or parachute deployment if the factor were 1.

Add up `events` and `expected_events` over every result for a part. The best estimate of its factor is

    (2 + total events) / (2 + total expected events)

This is the mean of the gamma posterior, with shape 2 + events and rate 2 + expected events. For example, a parachute whose drops logged 7 events where the model expected 2.9 has an estimated factor of 9 / 4.9 = 1.8. A few tests leave the estimate near 1 and uncertain. `reliability_after` in a result gives the model's chances after the test (`model_p_start`, `model_lambda_per_s`, `model_stage_first_ignition_p`, `model_deployment_failure_p`), never the real ones.

The verifier flies the real factors.

### When an engine fails

Not every engine failure destroys its stage. Many end with the engine shut down, and the rest of the stage could fly on. Apollo and Space Shuttle engine data put the share of liquid engine failures that are catastrophic at 20% to 40% (Huang, Fint and Kuck, "Key Reliability Drivers of Liquid Propulsion Engines and A Reliability Model for Sensitivity Analysis", AIAA 2005, NASA NTRS 20050207429). marsflight takes 30%:

- 30% of engine failures destroy the stage and lose the mission from that moment.
- 70% shut one engine down. A stage with one engine of that kind has lost that thrust, so this loses the mission too. A stage with two or more of them flies on with the engines left, if it has the thrust and the propellant to spare and your flight software adjusts the burn. Saturn V did this: on Apollo 13 an S-II engine shut down 132 s early, the other four burned 34.5 s longer, and the vehicle reached orbit.

The verifier never makes a part fail in the graded flights, and these chances do not enter the score. The survival it reports beside the score does not fly engine shutdowns, so there every engine failure counts as losing the mission, whether or not the stage has engines to spare.

In flight, a shutdown is an `engine_out` event with the element, the engine and `engines_left`, and `engine_out:S1` in the observation's `events`. In the observation's `elements`, each engine kind on a stage has `out`, the number shut down. An engine that is out stays out, and a command for more engines than are left fires the ones left. Your image has no simulator: fly a shutdown in your own models to see whether your vehicle and flight software survive it.

### When a parachute or an ignition fails

Two more failures can be survived by a design built for them, and the verifier checks them the same way:

- **A backup parachute.** If a parachute has another parachute on the same carrier (the backshell or capsule it rides on), the verifier flies the first graded world where it deployed again with it failing to open. 80% of parachute failures are of that kind, which a separate backup can recover; the other 20% defeat the backup too (NASA's common-cause failure share for redundant hardware, NTRS 20160007073). In flight, the failed canopy is a `parachute_failed` event, `parachute_failed:CHUTE` in the observation's `events`, and `deployed` and `failed` in its `elements` entry; it adds no drag. Deploy the backup.
- **A retry after a no-light.** If every engine on a stage can start again (two starts or more: a spark torch, hypergolic propellants, or several TEA-TEB cartridges), the verifier flies the first graded world where the stage fired again with its first ignition a no-light. 60% of stage ignition failures are no-lights that a retry recovers; the rest damage the stage, or have a cause a retry does not clear (NASA launch records, NTRS 19910018896 and 20170009844). In flight, it is an `ignition_failed` event and `ignition_failed:S2` in `events`, and the start is spent. Command the stage again; a pump-fed engine in free fall needs its propellant settled first (`settle`).

Each check keeps a share s of the value its graded twin earned after the failure, and the part's failure chance then counts with 1 − 0.8 × s for a parachute or 1 − 0.6 × s for a stage's first ignition. A part with no backup, or a stage with an engine of one start, gets no check, and its failures lose the mission. The checks fly after the graded flights, only if at least 600 s of the verifier's time budget remain, and each gets at most three times its twin's wall-clock time.

## The flight software: `flight/guidance.py`

```python
def reset(info): ...          # once: info has "t0", "t", "sites", "seed_is_nominal", "scenario", "design", "vehicle"
def step(obs) -> dict: ...    # every arc
```

It runs in its own process with no network, as an unprivileged user. You may import numpy, scipy, tudatpy and your own modules in `flight/`. It may also read Tudat's public data, which the verifier has too: the SPICE kernels in `$MARSFLIGHT_KERNELS` (planet ephemerides, frames and constants), loaded with `tudatpy.interface.spice`. Nothing else is on its path, and the verifier's own files are closed to it, so any other data or model it needs (atmosphere tables, constants, calibrations) must be in `flight/`.

The verifier runs it from a copy of `flight/` in an empty folder. Nothing else of the repository is there, `program/test_results.json` and `design/` included. So anything the flight software needs, such as measured drag, lift, Isp and masses, must be inside `flight/`.

`info["t0"]` is the launch time and `info["t"]` the time now, `info["sites"]` the launch site, Jezero and the Utah range, `info["seed_is_nominal"]` is false in the verifier, and `info["scenario"]` is null. `info["design"]` and `info["vehicle"]` hold the program office's compiled form of your model and its build report. Their format is internal and may change, so do not rely on them.

There is no simulator in your image. Check your flight software against your own models of the vehicle and the mission, built with Tudat and CEA and corrected with your test results. The verifier is the first time it flies the program office's vehicle.

Limits:
- 20 s per call.
- In the verifier, each flight gets 1500 s of wall-clock time, simulation and flight software together. Past that, the flight fails where it is, and milestones already reached still count.
- At most 20,000 calls to `step` per flight (a whole mission takes about 700 to 1,600). Past that, the flight fails where it is, as at the time limit. Flight software that keeps asking for something the vehicle refuses, or for very short arcs, reaches it.
- The verifier flies several worlds at a time, as many as the grading machine allows (from 3 up to all 48), and must finish all 48 within 5400 s. If slow flights would push it past that, a flight that starts late gets only the time left. With many worlds at once, the flights may share CPUs and each one runs slower. Flight software that keeps each flight to a few minutes never meets these limits.
- Memory: each process of the flight software may hold at most 2 GiB of data (heap and private memory, `RLIMIT_DATA`). Past that, an allocation fails, which in Python is a `MemoryError`. The reference flight software needs about 0.6 GB. The flights flying at once share the grading machine's memory, which is planned for about 1 GB a flight: flight software that uses much more, or starts more processes, can fill it and get killed by the kernel.
- Processes: all flight software flying at the same time may have at most 32 processes and threads together, or 10 a flight when more than 3 fly at once (`RLIMIT_NPROC`). The flight software cannot raise either limit.
- numpy and scipy get one BLAS thread (`OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`). Raising it costs memory against the limit above.
- An exception, a hang or a bad reply ends the flight as a flight software failure. So does flight software that dies, from a `MemoryError`, from the kernel's out-of-memory killer or from any signal. A flight is flown once: one whose flight software fails is never flown again.

### `obs`

| Key | Meaning |
|---|---|
| `t`, `met` | Time (TDB s since J2000) and time since launch. |
| `mode` | `pad`, `flight` or `landed`. |
| `body` | Central body. |
| `r`, `v` | **Navigation** position and velocity (m, m/s), relative to the central body in J2000. They come from the onboard navigation filter, with realistic errors (see "Dynamics and navigation realism"). |
| `lat`, `lon`, `alt`, `height_above_surface` | Geodetic latitude and height above the WGS84 ellipsoid at Earth, areocentric latitude and height above the reference sphere at Mars (see "Time and frames"). `height_above_surface` is the height above the ground under the vehicle (see "Terrain and landing"). Below 20 km above the ground, `alt` and `height_above_surface` come from a radar altimeter with 1% noise. |
| `airspeed`, `airspeed_vector`, `mach`, `q` | Only in the atmosphere. `airspeed_vector` is the navigated velocity relative to the air, wind included. |
| `comms` | Your link to Earth: `dsn_contact` (an uplink is possible now), `downlink`, `sep_deg` (the Sun-Earth-probe angle), `owlt_s` (one-way light time), `moratorium` (solar conjunction: no commands below 2 deg) and `downlink_bps`. A correction burn in solar orbit is refused (a command error) during the moratorium or with no uplink in the previous 7 days. |
| `launch_azimuth_deg` | The launch azimuth range safety measured above 5 km. |
| `sensed_accel` | Non-gravitational acceleration over the last arc (accelerometer). |
| `attitude` | The current body axis, which is the thrust direction (unit vector, J2000). |
| `mass` | Estimated total mass. |
| `bank_deg` | The bank angle now (see `bank_deg` in the command). |
| `elements` | What is still attached: propellant left, engines with `starts_left`, `lit` and `out` (engines shut down by a failure), RCS propellant, parachute `deployed`, `inflated` and `failed` (deployed and never opened). |
| `events` | What happened since the last call, e.g. `burnout:S1`, `separation:S1`, `ignition_failed:S2`, `parachute_opened:CHUTE`, `engine_out:S1`, `soi_exit`, `atmosphere_interface`, `trn_fix`, `touchdown`, `milestone`. |
| `command_errors` | Commands that were rejected or clamped. |
| `surface` | Landing site while on the ground. |
| `samples_loaded` | Whether the samples are in the capsule. |

### The command you return

```json
{
  "engines": {"S1": {"throttle": 1.0, "count": 9}},
  "rcs": {"element": "C1", "throttle": 1.0},
  "settle": false,
  "dir": [x, y, z],
  "bank_deg": 0.0,
  "mode": "entry",
  "actions": ["separate:S1", "deploy:CHUTE", "jettison_ballast:HS", "isru:MAV1", "end:mission"],
  "next": 2.0,
  "wake": {"alt_below": 125000}
}
```

Every number that `reset` or `step` returns must be finite: a NaN or an infinity ends the flight as a flight software failure.

What each field does:
- **`engines`:** per element, `{"throttle", "count"}`, or a list with `"engine"` names (the engine part definitions, e.g. `R2`) for stages with several engine types. Elements not listed are shut down.
  - Throttle is 0, or between the engine's minimum and 1.
  - Each ignition uses a start and takes 1 s to reach thrust. Cryogenic engines also lose 0.5 s of propellant to chilldown.
  - `count` is how many engines burn. Raising it ignites the added engines: each one uses a start, counts as an engine start for reliability, and takes its own 1 s to reach thrust. The engines that light are the unlit ones with the most starts left. Lowering it shuts engines down, and raising it again restarts them. `starts_left` is what the most used engine has left.
  - **Pump-fed engines will not start in free fall unless the propellant was settled.** Settle it with `"settle": true` (RCS ullage thrust) in the arc before, or with thrust that is still acting. Settling lasts until 5 s after the end of the arc that fired it.
- **`dir`:** the commanded body axis and thrust direction in J2000. The vehicle turns towards it no faster than its slew rate. The slew rate is the fastest `maxSlewRate` among elements with gimballed engines or RCS, or 0.5°/s if there are none.
- **`bank_deg` and `mode: "entry"`:** an unpowered vehicle with a heatshield or capsule leading flies as a lifting body at its trim L/D, banked by `bank_deg`.
  - The vehicle rolls to the commanded bank the short way round and stops on it. It rolls at up to 20°/s, and it speeds up and slows down at up to 5°/s², as MSL did in its bank reversals (Way 2013). So a reversal from +60° to −60° takes 10 s.
  - A command that moves a little on every call is followed with a lag, because the vehicle slows down to stop on each one. Command the bank you want to end at.
- **`next`:** seconds until you are called again. The arc also ends early at:
  - a sphere-of-influence crossing;
  - entering the atmosphere (150 km at Earth, 130 km at Mars);
  - touchdown or impact;
  - burnout;
  - a `wake` altitude crossing.

  Arcs are capped at 10 s powered or 20 s unpowered in the atmosphere, 120 s when powered in vacuum, and 60 days when coasting.
- **`end:mission`** stops the flight. Use it for partial tests.

On the ground (`pad` or `landed`), time advances by `next`, at most 30 days per call. The vehicle lifts off once thrust exceeds 1.02 × its weight. On the pad, engines are held down until they reach thrust.

### Aerodynamics of the launch stack

A launch stack flies as a slender body when it carries a fairing, or when a stage is at its bottom and it does not fire against the airflow. Its aerodynamic force has two parts. Along the body axis, the axial force follows a Mach table for Saturn and Atlas class vehicles. Across the axis, the normal force is q × S × 2.5 × sin α, where S is the area of the widest diameter in the stack and α is the angle between the body axis and the airflow. The slope of 2.5 per radian is the Saturn V's without its fins, from wind tunnel tests (NASA TM X-53770, 1968). Fins are not modeled, so every stack gets this slope. The stack's hidden drag factor scales the axial force only.

So a stack that flies off the airflow gets lift, and a wind across its path pushes it sideways, downwind. The same angle sets the bending load on its tanks. `airspeed` and `airspeed_vector` are the navigated velocity relative to the air, with the wind at the navigated position taken out (see "Dynamics and navigation realism"). `sensed_accel` includes the lift and the side force. The attitude stays fixed within an arc while gravity turns the velocity, so a gravity turn flown with long arcs flies at an angle of attack. With 2 s arcs, a Saturn V class ascent reaches about 1.4° at max q.

An unpowered vehicle with a heat shield or capsule in front flies as a lifting body, as described under `bank_deg`. Anything else gets drag only, against the airflow.

## Terrain and landing

The ground at both landing sites comes from public maps:
- **Jezero:** heights from the USGS Mars 2020 terrain-relative navigation maps (HiRISE at 1 m and CTX at 20 m), averaged into 50 m cells, about 30 by 30 km around Jezero. Heights are above the MOLA areoid, which the simulator places at its 3389.5 km reference sphere. The ground at the target is about −2,570 m.
- **Utah:** heights from the USGS 3DEP 1 arc-second map, averaged into 4 arc-second cells (about 120 m) from 40.0 to 40.9°N and 113 to 114°W, above the WGS84 ellipsoid, at geodetic latitudes. At Earth `alt` is also the height above the WGS84 ellipsoid, so over Utah `alt` minus the map's height is the height above the ground.
- Utah hazards: the range's dry lakebed is sandy clay that a capsule sinks into, which takes up the impact. On the map it is the ground below 1,300 m with a slope under 5° at the cell scale (within the basin, steeper spots are bedrock outcrops and old islands), 79% of the map and all of the ground within 10 km of the target. The alluvial fans and mountain ranges around it are hard ground. A crushable capsule is designed for the lakebed, so landing on hard ground ends the flight. A parachute landing is not affected. The touchdown event gives `surface` (`lakebed` or `hard`), and on the lakebed `impact_g`, the capsule's peak deceleration as it sinks in: 30 × diameter × speed² / mass, the fit to drops of rigid penetrometers and full-scale Mars Sample Return capsules at the range (Kellas, NTRS 20170002221).
- Off the maps, the ground is flat at the site altitude (Jezero −2,550 m, Utah 1,300 m).

The ground under the vehicle sets its height above the surface, the radar altimeter reading and where touchdown happens. At Jezero, each 50 m cell also holds two hazard layers:
- `rough_deg`: how far the slope at a lander's own scale (2 m) scatters about the slope the cell's heights give, in degrees per horizontal direction. It is measured on the 1 m HiRISE map, so that the cell's share of ground steeper than 15° matches the map's. Off the HiRISE map it is estimated from the 20 m map.
- `rock_k`: the share of the ground covered by rocks, k in the Golombek and Rapp (1997) model. No public rock map of Jezero exists, so this layer is a model. Its k grows from 1% on the smoothest ground to 21% on the roughest, by the percentile of the 1 m roughness. The top value is set so that 99% of landings spread like Mars 2020's see k below 18.8% (Way et al. 2020).

What a lander meets at one spot is drawn from its cell's layers by a hash of the spot itself. The same spot always has the same slope and rocks, in every world. In the Golombek and Rapp model, rocks with diameter of at least D cover a share k·e^(−q·D) of the ground, with q = 1.79 + 0.152 / k. A rock's height is half its diameter.

The maps are in `/app/data/terrain/` (`jezero.npz` and `utah.npz`, numpy archives), the same files the simulator lands on. Each has `lat0`, `lon0` (the south-west cell center, degrees, east positive, areocentric at Mars and geodetic at Earth), `dlat`, `dlon`, and the integer layers `elev`, `rough_deg` and `rock_k`, to be multiplied by `elev_scale`, `rough_scale` and `rock_scale`. Rows run from south to north. The flight software can read only `flight/`, so copy a map or a hazard summary of it into `flight/` to use it onboard.

## 6-DOF mode (what the verifier flies)

The verifier flies the vehicle as a rigid body. The attitude is not kinematic: the vehicle does not simply turn towards `dir` at its slew rate, whatever its engines, RCS and aerodynamics could do. A design that would fly with a kinematic attitude can still lose control, hit its spent stage or run out of RCS here. `reset(info)` gets `info["dynamics"]`, which is `6dof` in the verifier.

**Mass properties.** The center of mass and the inertia come from the stack. Each element's dry mass is split into engines, tanks and structure at their places along the stack, and the propellant lies at the bottom of each tank. They change as propellant burns and as elements separate.

**Torques.**
- Engine gimbal. Every engine with a `gimbalRange` above 0 deflects up to that angle about its mount at the bottom of its stage. Several identical engines on a stage (a part with a count, such as `R2[9]`) also give roll torque. A single engine gives none, even next to an engine of another type. A fixed nozzle, such as the Raptor Vacuum's, gives no steering torque: a stage with only fixed nozzles steers with its RCS alone (or by differential throttling, below), and in dispersed worlds its RCS must also hold the torque of the thrust misalignment (below), which is the engine's thrust times the misalignment angle times the distance from the nozzle to the center of mass.
- Differential throttling. On a stage with differential throttling (`differential_throttle` in the design file), a cluster of two or more engines of one type steers in pitch and yaw by throttling engines on one side down and on the other side up. The engines sit evenly on a ring of 0.3 × the stage diameter. Each engine stays between its minimum throttle and full thrust, so a cluster at full thrust can only throttle down, and it loses some thrust while it steers. It burns only the propellant of its throttle settings. Two engines steer in one plane only, and a single engine cannot steer this way. Fixed nozzles give no roll torque. The Soviet N1 steered its first stage this way.
- RCS. Each element with a `ReactionControlSystem` (its `totalThrust` and `propellantLoad`) can use a quarter of its RCS thrust about each axis. The lever arm is the distance from the center of mass to the far end of the element, and the element's radius for roll. The propellant used is the thrust times the time, over 225 s × g0.
- Aerodynamics. A launch stack (a fairing on top, or a stage flying nose first) has a normal force of 2.5 per radian on its largest cross section. Slender-body theory places it: twice the nose's base area, two thirds of the way down the nose; twice each change of cross section, where it happens (a flare is stabilizing, a boattail destabilizing); and the rest spread along the body. A center of pressure ahead of the center of mass makes the stack unstable, and the controller must hold it. The pitch rate also damps through the same normal force. A bluff body has drag only, along the airspeed.
- Thrust misalignment. In dispersed worlds the per-arc pointing error tilts the thrust from the body axis, which makes a torque.
- Parachutes. A deployed parachute pulls along the airspeed at the top of the backshell or capsule that carries it, which holds the vehicle below it in line with the airflow.

**Actuators.** The actuators do not move at once:
- A gimbal moves towards its command no faster than its engine's gimbal rate: 5°/s for an engine of 4 MN vacuum thrust or more, as the Saturn V's F-1 actuators moved, and 8°/s for a smaller one, as the J-2's did. The engine report gives it as `gimbal_rate_deg_s`. Close to the command the gimbal follows with a lag of 0.074 s (a first-order response, from the gain of the Saturn servo loop). A big step therefore takes time: a 5° step takes about 1 s on a large engine.
- An engine's thrust follows a throttle command of differential throttling with a lag of 0.2 s, as the Space Shuttle main engine's chamber pressure did, and changes by no more than 137% of full thrust per second (the main engine's limit of 7,000 lb per 10 ms).
- RCS thrusters fire in pulses. A pulse lasts at least 22 ms, the on time at which the Aerojet MR-104H hydrazine thruster's minimum impulse bit is given. So the smallest change of body rate is a quarter of the RCS thrust × 22 ms × the lever arm / the inertia. A demand for less rounds to one such pulse or to none. So the RCS cannot hold the body rates at exactly zero: when it holds an attitude, the rates jump back and forth by about that amount.

**Attitude commands.** An onboard controller samples its sensors once per integrator step (0.1 to 0.5 s), holds its commands through the step, and turns your command into gimbal, throttle and RCS commands. It uses the gimbals first, then differential throttling, then the RCS:
- `"dir"`: an autopilot steers the body axis to it. It turns no faster than the slew rate, and no faster than its actuators can stop within the angle that is left. Roll is held at zero rate.
- `"attitude": {"rate_deg_s": [p, q, r]}`: a rate loop holds these body rates.
- `"attitude": {"torque_Nm": [L, M, N]}`: this torque, within what the actuators can give, with no feedback.
- `"attitude": {"bandwidth": w}`: the natural frequency of the loops, from 0.2 to 4 rad/s (default 1.5).
- `"attitude": {"deadband_deg": d}`: how far the direction may wander in a coast outside the atmosphere, from 0.1° to 10° (default 1°, the Saturn V third stage's).

A rate or torque command holds until a later command sends `dir` or another rate or torque.

**Body axes and what you observe.** x is the stack axis, from the bottom to the top (the thrust direction). y and z are across it. On the pad, x is up, y east and z north. Rates and torques are in body axes. `obs` also has `attitude_q` (the quaternion `[w, x, y, z]` that turns body axes into J2000), `body_rates_deg_s`, and `mass_properties` (the center of mass from the bottom of the stack, and the inertia about it). `attitude` is still the body x axis.

**Sensors.** In 6-DOF neither the flight software nor the onboard controller sees the true attitude, body rates, Mach number or dynamic pressure:
- `attitude_q` and `attitude` are the onboard estimate. Gyros carry it between star tracker fixes. Their bias is 1 deg/h per axis, drawn once per flight, and their angle random walk is 0.07 deg/√h (the LN-200 IMU). The star tracker (two heads, 5 arcsec per axis) fixes the attitude in every arc that starts outside the atmosphere with the vehicle turning slower than 3 deg/s. It is blind inside the atmosphere, so through an ascent or an entry the error grows with the gyro bias, to about 0.1 deg by the time an Earth ascent leaves the atmosphere. Before liftoff the estimate is aligned as well as a star tracker fix.
- `body_rates_deg_s` is what the gyros read: the true rates plus their bias.
- The controller steers the estimate, so the thrust points off by the estimate's error.
- `mach` and `q` come from the navigated `r` and `v` and an onboard atmosphere. That is the world's own model at the navigated point, without the world's density dispersion or dust storm, and with the nominal world's winds. `airspeed` and `airspeed_vector` are the navigated velocity relative to the air, wind included (see "Dynamics and navigation realism").
- In the nominal world every one of these errors is zero, as the navigation errors are.

**Which arcs are rigid body.** Tudat propagates the attitude in every powered arc and every arc in the atmosphere, except a lifting entry. A lifting entry (a heatshield or capsule leading, unpowered, `mode` `entry`) flies the trimmed model of 3-DOF, banked by `bank_deg`, with its heatshield into the airflow. In a coast outside the atmosphere, the RCS turns the vehicle towards `dir` with the torque it has: it speeds the turn up, holds a rate no faster than the slew rate, and stops at the target. It uses the slowest rate that still ends the turn within the arc. A turn that cannot end in time (a heavy vehicle on a small RCS turns slowly) carries on into the next arc. Rates across the turn, or away from the target, are stopped first, which takes time at the RCS's torque, and the vehicle keeps turning while they stop. A vehicle turning too fast to stop at the target overshoots it. Each change of rate costs inertia × change of rate / lever arm / (225 s × g0) of propellant, so a whole turn costs twice that for its top rate. A vehicle without RCS cannot turn in a coast: it keeps its body rates. Once the direction is within the deadband, the RCS holds it there in a limit cycle: at each edge of the deadband one shortest pulse (22 ms) reverses the body rate, so the vehicle swings across the deadband and back at half the rate change of one pulse, about each axis. This costs, per axis and per second, (rate change of one pulse) / (4 × deadband) pulses, each burning a quarter of the RCS thrust × 22 ms / (225 s × g0). A wider deadband costs less, in proportion, and points less exactly; a long cruise on a narrow deadband can use a lot of RCS propellant. The rate change of one pulse grows with the RCS thrust and shrinks with the inertia, so a light vehicle with a strong RCS pays the most. A rate command (`attitude.rate_deg_s`) in a coast is met in whole shortest pulses, so the body rates end within half a pulse's rate change of it, and then cost nothing more: the vehicle drifts slowly. On `"rate_deg_s": [0, 0, 0]` a vehicle can drift through a long cruise and turn to its next burn direction before the burn.

**Separation.** A separated stage, heatshield or backshell is flown next to the vehicle for 60 s, or until it is 100 m clear and moving away. It has the same gravity, its own drag as a tumbling body in the same wind (and its parachute's, for a backshell) and a 0.5 m/s push from the separation springs. Separating a stage does not shut its engines down: a stage that still burns keeps its thrust until its propellant runs out. A shutdown in the same command as the separation comes first. After a shutdown or a burnout the thrust tails off: it falls by a factor e every 0.25 s, so 5% is left after 0.75 s (Apollo 11's S-II stage, AS-506 flight evaluation report MPR-SAT-FE-69-9, section 6.4). A stage separated during its tail-off still pushes. Your own vehicle does not feel the tail-off. If it hits the vehicle again, the flight ends. Fairing halves are not tracked. In dispersed worlds each separation also gives the vehicle a random tip-off rate, 0.3 deg/s per axis. The event `separation_clear:ID` says when the tracking ends. In `events.json` it has the time since the separation (`after_s`), the gap then (`gap_m`), and the smallest gap after the gap stopped growing (`min_gap_m`, null if it only grew).

What can go wrong only in 6-DOF:
- Losing control. An unstable stack whose actuators cannot hold its angle of attack turns sideways, and the loads (below) break it. An unpowered stage in the atmosphere has only its RCS.
- Recontact after a separation. Shut a stage's engines down, coast about 1.5 s while the thrust tails off, then separate it (a burnout counts as a shutdown). Separating at the moment of shutdown or burnout lets the tail-off push the spent stage into the vehicle, unless the next stage is already pushing away. Apollo 11 lit its second stage's ullage motors 0.5 s after first-stage cutoff and separated the first stage at 0.7 s, firing retrorockets on it (MPR-SAT-FE-69-9, table 2-2).
- Running out of RCS propellant, after which a coasting vehicle cannot turn.

## What can go wrong (each ends the flight)

- **Range safety.** The Eastern Range allows launch azimuths of 37 to 114 deg from LC-39A. In powered ascent the instantaneous impact point, and the impact point of each spent stage or fairing, must not fall on land between 10 and 2,000 km from the pad.

- **Loads.** The capsule's `DecelerationLimit`; more than 150 g breaks anything. Both apply to the acceleration the vehicle feels, thrust and drag added as vectors. Stage tanks buckle when `(axial load + bending from q·α) × factor of safety` exceeds the buckling load times the real buckling strength factor. Winds give angle of attack at max-q. A fairing carries the aerodynamic bending and its own drag around the payload it covers, so stages under it see only the mass above them.
  - The axial load on a tank is the mass above it times the axial acceleration the stack feels: thrust minus the drag along the axis. When the stack flies nose first, the drag acts on the leading element, so every tank below it also carries that drag. When it flies tail first, as in retropropulsion, the drag adds to the thrust instead.
  - The check runs through every powered phase, in the air and in space. A stage's highest load usually comes at burnout, when the acceleration peaks.
  - A balloon tank's pressure alone must carry the load.
  - A stiffened tank holds the axial load its stiffened wall carries (SP-8007's stiffened cylinder method), plus the pressure end load.
- **Tank burst** at launch, when a tank's real welds cannot hold its hoop stress (see "The hidden truth").
- **Engine flaws.** An unstable engine design is destroyed at its first ignition. A design whose real cooling margin is below 1 burns through once that engine has fired over 20 s, counting every burn.
- **Heating.** Stagnation heating is Sutton-Graves convective plus Tauber-Sutton radiative (Earth above 9 km/s, Mars above 6 km/s). It stands for the hottest point of the heat shield.
  - The convective term is multiplied by a factor fitted to flight data. It is 1.11 at Earth, from Stardust's as-flown reconstruction, and 1.0 at Mars, from the hottest plugs measured on MSL and Mars 2020. Then the whole flux is multiplied by a margin factor of 1.0, so there is no deliberate margin.
  - Above 0.3 MW/m², the bottom-most element must be a heatshield or capsule, or the vehicle burns up. Behind a heatshield, only the elements inside its aeroshell, its backshell and the parachutes on them are protected. Anything else still attached burns up. Behind a capsule, only the capsule and its own parachutes are protected.
  - Heat flux above what the TPS material really survives fails it.
  - The integrated heat load must stay below what the TPS layer absorbs. A thicker layer absorbs more. The arc-jet measures it for your layer.
- **Fairing jettison** above 1135 W/m² free-molecular heating overheats the payload. **Heatshield separation** must be subsonic (Mach < 0.9).
- **Parachutes:** deploying outside the Mach or q envelope fails them. A peak opening load above what the canopy holds tears it (see `Parachute` under "The design").
- **Nozzle flow separation** when firing in the atmosphere.
- **Loads above the atmosphere.** The capsule's `g_limit`, the 150 g limit and the stage tank buckling check run on every powered arc, also above the atmosphere and in solar orbit, where the thrust alone loads the vehicle. The thrust load peaks at burnout, when the stage is lightest, so a high thrust burn in space can break a capsule's limit or buckle a tank under a heavy payload.
- **Mars touchdown:**
  - The bottom element needs legs.
  - Vertical speed must be at most 1.25 × the legs' rating, and horizontal speed at most 1.5 m/s.
  - The body axis must be within 15° of vertical.
  - The ground at the lander's scale must slope at most 15°, or the lander tips over (InSight's limit at 2 m, Golombek et al. 2017).
  - No rock under the lander may be taller than 0.35 m. The lander covers a circle of its diameter. InSight allowed 45 cm under a footpad, less 10 cm when the legs crush.
- **Earth touchdown:** only the bare capsule, and its parachute, may arrive. It must touch down at up to 10 m/s under a parachute, or up to 45 m/s for a crushable landing, which must also come down on the dry lakebed (see "Terrain and landing").
- **Power.** Running short for more than 3 days in a row loses the vehicle. Short power freezes storable propellants (NTO freezes at 262 K, MMH at 221 K), and heaters must keep them 10 K above freezing. On Mars, solar-only vehicles need batteries for the night, and dust storms cut solar power.
  - Cryogens boil off at the heat leak minus what the cryocoolers lift. This is not a failure; you just lose propellant.
  - MLI in Mars's CO₂ atmosphere performs about 8× worse than in vacuum.
- **Impact** anywhere except a valid touchdown.

The failure chances of "Reliability" for engines, stages and parachutes do not end a graded flight and do not enter the score; the verifier reports them beside it as the chance that the flight got as far as it did. Other parts do fail during the flight: avionics strings, RCS branches, batteries, ISRU units, the landing radar, separations, latent flaws and aged propulsion. See "Failures, fault protection and the program office".

## Dispersions (the verifier draws fresh seeds you never see)

| What | Spread |
|---|---|
| Earth atmosphere | density ×(1 ± 5%), jet-stream winds 30 ± 15 m/s from a random direction; daily space weather for NRLMSIS 2.1: the forecast solar flux, with each day's geomagnetic index and flux variation taken from the observed record at a similar level of solar activity |
| Mars atmosphere | altitude-correlated density perturbations at the scatter measured between six flown entries (one sigma in log density: 5% up to 20 km, 8% at 30 km, 24% at 50 km, 38% at 70 km, 59% at 90 km, 60% above 130 km); seasonal pressure cycle; the observed dust of one real Mars year (24 to 36, from the Montabone et al. dust maps), by place and season, so storms come in their real season (Ls 180 to 330) and Jezero's dust is 0.6 to 1 in the dusty season; the density changes with the dust by height as below; winds 10 ± 8 m/s |
| Heating, aero | heating ±8%, Cd ±2%, L/D ±4%, parachute Cd ±3%, the opening load factor kP of each parachute opening 0.752 ± 0.080 |
| Hardware | each element's dry mass ±1%, thrust ±0.5%, Isp ±0.2%, pointing 0.05° per arc |
| Navigation | as listed under `obs` |

These are flight-to-flight variations. They come on top of the hidden truth, which is the same in every flight.

Some worlds are rarer and harder than the table:
- **A dust storm when you arrive** (about 1 world in 10). A global dust storm is already under way when the vehicle enters Mars's sphere of influence. It began up to 30 sols earlier and lasts 60 to 150 sols in all, at an optical depth 3 to 5 above the usual 0.5. So it covers the entry and at least the first 30 sols on the surface. It cuts solar power on the surface to 5% to 17% of its usual level, and it changes the density by height (below). It replaces any other storm of that world.
- **High winds at the landing** (about 1 world in 10). The winds over Jezero blow at 25 to 40 m/s instead of 10 ± 8 m/s, with the same profile: full speed from about 1.4 km above the ground, 30% of it at the surface. A vehicle under a parachute drifts with them, so it reaches the ground several km downwind, moving sideways at about the wind speed.

Dust warms the middle atmosphere, which expands, so in a dust storm the density changes with height: about 11% less near the ground, about the same at 15 to 20 km, about 1.25 times at 27 km, 2.3 times at 50 km and 3.6 times above 70 km. The shape comes from the six flown entries, whose densities rise with the dust opacity at the time by height (0 near the ground, 0.27 per unit of optical depth at 15 to 40 km, 1.05 at 40 to 60 km, 1.6 above 60 km), and the effect saturates in thick dust, as dust heating does.

The two rare draws are drawn separately, so about 1 world in 100 has both. Your flight software is not told which world it is in.

## Milestones and score

Each flight earns the weights of the milestones it reaches. They count in order, except the two landing-target milestones, which don't block later ones:

| Milestone | Weight | Reached when | Partial credit before it is reached |
|---|---|---|---|
| `design_valid` | 0.017 | the model compiles and passes the build review (`design_review`) | none |
| `liftoff` | 0.017 | above 100 m | none |
| `max_q_passed` | 0.025 | above 50 km on ascent | altitude / 50 km |
| `earth_orbit` | 0.042 | perigee above 150 km, or escaping | the mean of the orbital energy gained (from the pad to a 150 km circular orbit) and the periapsis height / 150 km |
| `earth_departure` | 0.042 | leave Earth's sphere of influence | the energy gained from a 150 km orbit toward escape |
| `mars_arrival` | 0.051 | enter Mars's sphere of influence | the closest approach to Mars on a log scale: log(d0 / d) / log(d0 / 577,000 km), where d0 is the distance to Mars as the cruise starts (leaving Earth's sphere of influence) |
| `mars_entry` | 0.068 | slowed below 1 km/s under 125 km, intact | the share of the entry kinetic energy removed while intact, from the speed at 130 km down to 1 km/s (booked only at moments the vehicle survived its load and heating checks) |
| `mars_descent` | 0.042 | within 2 km of the ground below, intact | the share of the height from the end of entry down to 2 km above the ground |
| `mars_touchdown` | 0.084 | a valid landing. The samples wait in a cache at Jezero: a lander that comes down more than 10 km from the target cannot reach them, and the flight ends there | for a crash, 0.5 × (1 − excess / 30). The excess is in m/s over the vertical limit, plus any horizontal speed over 1.5 m/s, plus 1 m/s for every 5° of tilt over 15°, 1 m/s for every 5° of ground slope over 15°, and 1 m/s for every 10 cm of rock over 0.35 m. |
| `mars_target` | 0.12 | earned by every valid landing, by its distance to Jezero (18.4447°N, 77.4508°E): 1 / (1 + (miss / 1 km)²) of the weight | (the credit is the distance rule) |
| `samples_loaded` | 0.042 | 30 sols on the surface with power and thermal control holding; then the capsule's `sampleContainers` of samples (11 kg each) go into it | sols of surface work / 30, with the capsule on the lander |
| `mars_orbit` | 0.068 | after Mars liftoff, periapsis above 100 km, or escaping | as `earth_orbit`, for a 100 km Mars orbit |
| `mars_departure` | 0.051 | leave Mars's sphere of influence with the capsule and samples | the energy gained from a 100 km orbit toward escape |
| `earth_arrival` | 0.051 | enter Earth's sphere of influence | the closest approach to Earth on a log scale, as for `mars_arrival`, from the distance to Earth as the cruise starts to its 924,000 km sphere |
| `earth_entry` | 0.076 | capsule slowed below 500 m/s, intact | the share of the entry kinetic energy removed, from the speed at 120 km down to 500 m/s |
| `earth_touchdown` | 0.084 | a valid capsule landing within 50 km of the Utah target, inside the range | for a crash, 0.5 × (1 − (speed − limit) / 30); for a landing outside the range, 0.5 × 50 km / miss distance |
| `earth_target` | 0.12 | earned by every valid landing inside the range, by its distance to the Utah Test and Training Range target (40.36°N, 113.52°W, geodetic), measured over the ellipsoid: 1 / (1 + (miss / 5 km)²) of the weight | (the credit is the distance rule) |

The simulator tracks these milestones and their partial credit for every flight. A world's progress is the sum of the weights of the milestones its flight reached, in order, plus the weight times the partial credit of the first one it did not reach. The two landing targets, `mars_target` and `earth_target`, add their credit without stopping the count. Their credit is set by how close the landing is, not by a radius: a landing 1 km from Jezero earns half of `mars_target`, 0.3 km earns 0.92 and 3 km earns 0.1; a capsule 5 km from the Utah target earns half of `earth_target`, 2 km earns 0.86 and 10 km earns 0.2. Together they are 0.24 of the score. The events `mars_target` and `earth_target` in a flight's milestones still mark a landing within 3 km of Jezero and within 10 km of the Utah target. A capsule that lands intact outside the Utah range earns 0.5 × 50 km / miss distance of `earth_touchdown`, and no `earth_target`.

**Task score:**

    score = G × the mean progress over the 48 worlds

- G is 1 when the correctness gate passes, and 0 when it fails.
- The progress of a world is what its flight physically did. Hardware failure chances ("Reliability") do not weight it, and the cost counts only through the budgets.
- So the score is between 0 and 1, and it is 1 when every world reaches every milestone, both landing targets included.

**The correctness gate.** The score is 0 if any of these fails:
- the model in `design/**/*.sysml` and `flight/guidance.py` are on `main`, and the model compiles;
- the design passes the build review (what `design_review` checks) and its hardware is within the budget;
- every result in `program/test_results.json` on `main` matches an outcome the program office gave the team: the same test kind and part key, and the same numbers, to within rounding, wherever the office's result has them (measured values, failure times, a simulated world's milestones). Fields of your own in an entry, such as notes, are not checked. Leaving results out is fine; a result the office never gave, or a changed value, fails the gate. The `results` branch that `share_result` writes is not checked: only the office writes it.

The verifier flies 48 dispersed seeds, then the failure checks (see "When a parachute or an ignition fails"), and `details.json` also reports each phase's pass rate, the share of full successes, the mean of the worst 10% of worlds, and the hardware's survival; none of these change the score.
## The realism that is active (read this before you design)

You never see the program office's simulator. You learn what the world does only from this manual and from the program office's tools and tests. Every model below is active in every verifier flight, so design your vehicle and your flight software for all of them. The sections after this one describe each model: what happens physically, the published size of the effect, what the flight software observes and can command, what the design can choose, and which program office test reveals the hidden part.

The manual never gives a hidden value: no exact allowable, no hidden truth of a part, and no draw of a single world. It gives the published ranges those come from. Your tests measure the rest.

| Area | Active models | Section |
|---|---|---|
| Propulsion | engine start and shutdown transients, chilldown, mixture ratio command and low throttle Isp loss; turbopump power balance and combustion stability drawn at every start; nozzle flow separation against the real ambient pressure; tank load spread, propellant gauges, low level cutoff and running dry; slosh and baffles; storage ageing; the cruise burn uplink rule; engine Isp and mass refits against flown engines | "Propulsion realism" |
| Structures, vibration and mass | tank walls under axial load, bending and shear, cold contraction, common bulkheads; liftoff, ascent and Mars liftoff noise; pyroshock through joints; pogo of the stack and of a center engine; fins; parachute canopy mass; hidden as-built mass growth | "Structures, vibration and mass realism" |
| Dynamics and navigation | rigid body (6-DOF) flight in every powered and atmospheric arc, entry included; coast disturbance torques; measured actuator data; the navigation filter (IMU, GPS, DSN passes, landing radar, terrain relative navigation); air data with the wind; orbit decay | "Dynamics and navigation realism" and "6-DOF mode" |
| Environment | NRLMSIS 2.1 Earth atmosphere with daily space weather; Earth winds; the Mars atmosphere with the Montabone dust maps; Mars winds; DE440 ephemerides; JGMRO_120F Mars gravity; the Jezero 20 m HiRISE terrain; launch weather from pad reports; range safety | "The environment and its data" |
| Power, thermal, comms and samples | RTG decay, solar arrays in space and on Mars with dust, batteries through every eclipse and night; heaters and boil off; the MOXIE fitted oxygen plant; DSN contact and the solar conjunction; sample mass, sample temperature and planetary protection | "Power, thermal, comms and samples realism" |
| Failures | random part failures in flight (avionics strings, RCS leaks, battery strings, ISRU units, landing radar, separations) with redundancy and fault verbs; the flown-failure latent flaws; engine-out, parachute and no-light checks; booster recovery; supersonic retropropulsion, plume and backshell effects; investigations of failed tests | "Failures, fault protection and the program office" |

Where an older section of this manual gives a simpler rule than these sections, the rule in these sections is the one that flies.
## Propulsion realism (active in every flight)

The engines and tanks behave like flown hardware, not like ideal ones. This section lists each effect, how large it can be, what your flight software sees, what it can do about it, what your design can choose, and which test shows you the part you cannot see. Numbers given here are published values from flown vehicles. The values drawn for your own hardware are hidden.

### Engine start and shutdown transients

An engine does not reach full thrust at once, and it does not stop at once. Each engine follows the start and shutdown curves of a flown engine of its kind, stretched to its own size:

| Kind | First thrust after the start command | Flown engine | Cutoff impulse scatter, one sigma |
|---|---|---|---|
| Pump-fed (gas generator, staged, full flow, electric pump) | 1.0 s | J-2 on the S-IVB | 6% |
| Expander | 0.5 s | RL10 on Centaur | 3% |
| Pressure-fed | 0.5 s | Apollo service propulsion engine | 9% |

Propellant flow follows thrust along the curves. After a cutoff command the thrust tails off along the engine's shutdown curve, and the impulse of that tail-off changes from cutoff to cutoff by the scatter above. A burn that must end on a precise velocity therefore needs its cutoff command early by the tail-off impulse, and it still misses by the scatter.

- **Observe:** `events` has `ignition`, `shutdown` and `burnout`. `sensed_accel` shows the thrust building up and tailing off.
- **Command:** `engines` as in "The command you return". Plan cutoffs with the engine's `shutdown_impulse_s`.
- **Design:** nothing extra. `design_review` reports each engine's `start_delay_s`, `start_full_thrust_s`, `start_profile`, `start_impulse_s`, `shutdown_profile`, `shutdown_impulse_s` and `shutdown_impulse_sd`.

### Chilldown

A cryogenic engine must cool its pumps and lines before each start. The propellant used for that is lost.
- With `chilldown = ChilldownMode::dump` (Centaur's way), the loss is about 1 s of full flow for a start soon after the engine was last cold, growing towards about 3 s after many hours, as the RL10's prestarts grew on long coasts. On the Earth pad the ground chills the engines, so the first start there loses nothing.
- With `chilldown = ChilldownMode::recirculation` (the S-IVB's way), the chill flow goes back to the tanks and a start loses only 0.05 s of flow. The chilldown pumps draw 200 Wh per start from the stage's battery. Without that charge, the chill flow is dumped and `command_errors` says `battery too low for the chilldown pumps`.
- **Design:** `chilldown` on any `RocketEngine` (developed or procured). `design_review` reports `chilldown_loss_s` and, for recirculation, `chilldown_battery_Wh_per_start`.

### Mixture ratio command and throttled Isp

- A developed engine may run below its design mixture ratio, down to 78% of it, as the J-2's propellant utilization valve did. The thrust and flow fall and the Isp rises a little. Command it per engine group: `"engines": {"S2": {"throttle": 1.0, "count": 1, "mixture_ratio": 4.5}}`. A ratio outside the range is clamped, with a `command_errors` entry. `design_review` gives `mixture_ratio_range` and the performance `at_min_mixture_ratio`.
- A stage whose tanks are not loaded at the engines' ratio sets `loadedMixtureRatio` on the `Stage`. Use it when the flight software plans to shift the ratio during the burn, so both tanks run out together.
- Isp falls at low throttle, beyond what the lower chamber pressure gives, because the injector mixes worse at low flow. The loss is about 1% below about 45% throttle (the Apollo lunar module descent engine's acceptance tests).

### Turbopump power balance and combustion stability

- A pump-fed engine must balance its turbine power against its pump power, at the pump and turbine efficiencies of flown engines of its cycle. `design_review` reports `pump_power_kW`, `turbine_power_kW` and `power_balance` with its margin. An engine that cannot balance is rejected.
- Combustion stability follows Hewitt's correlation. `design_review` reports each developed engine's `stability`: `chamber_diameter_m`, `first_tangential_Hz`, `strouhal`, `strouhal_required` and `margin`. A pintle injector counts double. The lower the margin, the higher the chance that one start goes unstable. **The chance is drawn at every start of every engine**, so an engine that starts many times is exposed many times. An unstable start destroys the engine and its stage.
- **Test:** `engine_hotfire` fires the real engine. An unstable start in the test is reported as a failure, which tells you the margin is not enough. Many starts in test give a better estimate. Catalog engines have no instability.

### Nozzle flow separation

A nozzle that is overexpanded in the atmosphere separates, and the side loads break it. The check uses the real pressure of the atmosphere model at the vehicle's place and time (NRLMSIS 2.1 at Earth, the Mars model at Mars), not a standard atmosphere. `design_review` reports `sea_level_separates` and `exit_pressure_kPa` for each engine. Firing an engine with a large area ratio low in the Earth or Mars atmosphere ends the flight.

### Propellant loading, gauging and the low level cutoff

- **Load spread.** Each tank holds its planned load times a factor of normal(1, 0.27%), clipped at 3 sigma. This is the spread between loaded and predicted mass on nine Saturn V stage flights. The factor of your tanks is hidden. Carry a flight performance reserve: Saturn V carried about 1% of the S-IVB load.
- **Gauges.** Set on the stage with a `PropellantGauging` part: `gauge = PropellantGauge::capacitance` (the default), `pointSensors` or `pvt`. Published accuracies, one sigma, as a share of the full load:
  - `capacitance` (S-II, S-IVB, Centaur, Shuttle OMS): 0.05% + 0.25% × fill. It reads only with the liquid settled, at an axial acceleration of at least 0.01 m/s² for 15 s, or on the ground. Between readings it holds the last one, so boiloff in a coast shows at the next burn. It is blind below 3% of the load.
  - `pointSensors` (S-IC, S-II): within 0.03% when the level passes a sensor (at 95, 80, 60, 40, 20, 10, 5 and 2% of the load), with flowmeters in between that drift by 0.5% of the propellant used since the last sensor. Needs settled liquid too.
  - `pvt` (helium pressure, volume and temperature, Shuttle RCS): works in zero g, but its error is 1.0% to 1.3% of the load for fuel and 1.1% to 1.8% for oxidizer, growing as the tank empties. RCS supplies always use it.
- **Low level cutoff.** `lowLevelCutoff = true` (the default) stops the engines when either tank's usable propellant falls to 0.15% of its load, before a pump runs dry. The stage counts as burned out there, and it can restart if it has propellant and starts left.
- **Running dry.** With `lowLevelCutoff = false` the engines run until a tank is empty, and what happens then depends on the cycle: a pressure-fed engine just stops and can restart; expander and electric pump engines stop and cannot restart; a gas generator engine stops if the oxidizer runs out first and burns its turbine if the fuel runs out first; staged combustion and full flow engines overspeed and are destroyed.
- **Observe:** each stage in `elements` has `fuel_kg`, `ox_kg` and `rcs_kg` (kg). Treat them as gauge readings with the accuracies above, and design your burn logic for that. `events` has `burnout:ID` and `propellant_depleted` with the `tank` that ran out, `cutoff` (`low_level` or `depletion`) and `restart` (whether the stage may start again). A stage that cannot restart answers a start with the `command_errors` entry `the engine cannot restart after its tank ran dry`.
- **Test:** `mass_properties` weighs the element dry. No test measures the load factor of a flight article: guidance that ends burns on velocity, not on propellant, does not need it.

### Propellant slosh and ring baffles

In 6-DOF, the liquid in each tank swings as a pendulum (the first slosh mode of NASA SP-106), and its swing turns the vehicle. Smooth walls damp it little. Flat ring baffles damp it more (Miles' formula) and add their mass. The swing is followed only while the liquid is held at the bottom of its tank; in a coast it stops, and the next burn starts it again from rest.
- **Observe:** the controller feels it as a disturbance torque in `body_rates_deg_s` and `attitude_q`.
- **Design:** a `RingBaffles` part in a tank: `widthFraction` (0.05 to 0.3 of the tank radius) and `spacing` (0.1 to 20 m). Their mass is in the stage's mass report as `ring_baffles`.

### Settling before a pump-fed start

A pump-fed engine will not start in free fall unless the propellant was settled (see `settle` in "The command you return"). A start without it is `ignition_failed` with the reason `pump-fed engine started in free fall without ullage settling`.

### Storage in space

Valves, seals and regulators age in space. Each stage that carries propellant can fail at an engine start, at the rate of fatal chemical propulsion anomalies on spacecraft launched from 2005 to 2015, 3.5 per million spacecraft days (Saleh et al. 2017), a constant hazard from launch. Over a 2.6 year mission that is about 0.3% per stage. A start after the failure time ends the flight with `propulsion failed to start after N years in space`. No test shows it; fewer stages that must start late carry less of this risk.

### Correction burns need the ground

A burn in solar orbit must be commanded from Earth. An engine start in heliocentric flight is refused, with a `command_errors` entry `correction burn not uplinked`, when there was no DSN uplink in the previous 7 days, or during the solar conjunction moratorium (Sun-Earth-probe angle below 2°). Read `comms.dsn_contact`, `comms.sep_deg` and `comms.moratorium` in `obs` and plan the burns around them.

### Engine model fits

The program office's engine model is calibrated against flown engines. Vacuum Isp follows a line in the log of the area ratio fitted to 20 flown regeneratively cooled engines. The dry mass of an engine you develop comes from a fit to 36 flown engines, by cycle, thrust and chamber pressure, with a factor for hydrogen; the fit's typical error is about 20%, which is why the built engine's mass differs from the model (the `mass_properties` test weighs it). Expander engines have a thrust cap per fuel, and an MMH regenerative chamber's heat load is calibrated on the flown Aestus. Catalog engines keep their published Isp and mass. `design_review` shows the result for your engine: `isp_vac_s`, `mass_kg`, `cooling_margin` and any errors.
## Structures, vibration and mass realism (active in every flight)

Every model in this section is on in every verifier flight. None of them gives the flight software a reading of its own. They end the flight with a message (for example `stage S1 buckled: ...`) when the hardware breaks, so the design must carry the margin and the flight software must keep the vehicle inside the envelope the design was sized for. The hidden part of each model is a property of the built hardware. A program office test measures it, as listed in each part below and in "The program office and R&D tests".

### Tank walls under axial load, bending and shear

The stage tank check is not axial only. Each tank barrel carries the axial load, the bending moment from the normal force at angle of attack, and the transverse shear, all together. The method is NASA SP-8007 (1968, kept in Rev. 2 of 2020): the axial and bending knockdowns, the gain from internal pressure, and the linear interaction of the three loads. Transverse shear buckles at 1.25 times the torsion stress (Lundquist, NACA TN 523). Factors of safety follow NASA-STD-5001B: 1.4 on ultimate for metal structure and 2.0 for bonded joints at a discontinuity. The tank factor of safety of 1.25 (qualified) or 1.6 (untested) still applies to the tanks, as in "The hidden truth of the hardware".

- What it means for the flight software: the load grows with q × α. Keep the angle of attack small through max q, and remember that burnout gives the highest axial load of a stage.
- What the design chooses: `PropellantTank` (`material`, `radius`, `wallThickness`, `barrelLength`), `PressurizationSystem.meop` and `balloonTanks`, and `StringerRingStiffening` (`stringerHeight`, `stringerThickness`, `stringerSpacing`, `ringSpacing`, and the optional flange and ring sizes).
- What reveals the hidden part: `structural_qualification` reports the real weld efficiency and the buckling strength against the SP-8007 design value.

### Cold contraction of cryogenic tanks

A tank wall shrinks when it is cold. A tank drawn at room temperature holds less propellant at cryogenic temperature. The contraction follows the NIST cryogenic property fits for aluminium alloys, 301 stainless steel and Ti-6Al-4V. A carbon laminate barely contracts and is left at zero. So size cryogenic tanks with this loss in mind. Nothing about this is hidden.

### Common bulkhead between hydrogen and another propellant

A `CommonBulkhead` part in a stage puts one dome between the two propellants inside one tank body. Between liquid hydrogen and another propellant it is a honeycomb sandwich, as on the S-II and S-IVB: two aluminium face sheets bonded to a fiberglass phenolic core. The face sheets are sized for burst with one tank full and the other vented. The core is sized so that the dome does not collapse under the same pressure difference the other way, with the knockdown of NASA SP-8032 for spherical caps. Between two propellants that are not hydrogen it is a plain dome, as on Falcon 9. Both tanks must then have one radius. Nothing about this is hidden; its mass is in the element's mass breakdown.

### Heat shield bond line in the cold

An ablator bonded to an aluminium structure is soaked at the cold limit before entry, which is −130 °C, the MSL cold qualification limit. The structure shrinks more than the ablator, and the bonded joint must take the strain with its factor of safety of 2.0. A layer that cannot take it cracks and ends the flight at the first heated point of its entry, with the message `heat shield cracked in the cold before entry`. The strain the material takes before it fails is a property of the built layer, with real scatter.

### Crack growth in tanks that wait on Mars

A pressurized tank of storable propellant that sits on Mars sees one pressure cycle each sol, as the sun warms its ullage. A small flaw that the acceptance proof test let through grows a little in each cycle (Forman crack growth). The tank must last four times the number of sols it waits (the service life factor of NASA-STD-5001B). A tank that does not leaks or bursts at the first arc in the air after Mars liftoff. Thicker walls, a lower `meop`, a material with more toughness or a shorter stay on the surface all help. Cryogenic tanks are not checked.

### Nozzle clearance at stage separation

When a stage separates, the engines of the stage above must come out of the interstage without touching it, while that stage turns at its separation tip off rate. A cluster of nozzles wider than the interstage, or long nozzles on a stack that swings, strike it: `engines struck the interstage at separation`. The design sets the gap: the stage diameter, the engine count and the nozzle size. The tip off rate of one separation is random.

### Liftoff noise, ascent noise and pyrotechnic shock

The rocket exhaust at liftoff radiates sound (NASA SP-8072). Inside the fairing it falls with distance from the nozzles and through the fairing's noise reduction. The level is calibrated to the Falcon 9 user's guide, whose maximum predicted level inside the fairing is 131.4 dB overall, and checked against the Atlas V 400 and 500 series. The random vibration of hardware is driven by this sound, so the acoustic qualification level covers it. Three more sources of noise and shock are active:

- Aerodynamic noise in ascent at transonic speed and at max q (NASA-HDBK-7005, section 4.5.1.1), through the same fairing noise reduction. It grows with q, so a steep, fast ascent through max q is louder.
- Liftoff noise on Mars, in the thin Mars air of the launch site. It is far quieter than on Earth for the same engines, but it is checked.
- Pyrotechnic shock at every separation plane. The two elements beside the plane feel the full clamp band level of the Falcon 9 user's guide, 1,000 g peak shock response. Elements further away feel 40% less for each joint in between, up to three joints (NASA-HDBK-7005 section 5.3.2).

An element that hears more noise than its acoustic level, or feels more shock than its shock level, plus a hidden margin, breaks: `broke in the liftoff noise`, `broke in the ascent aerodynamic noise` or `broke in the separation shock of ...`.

- What the flight software observes: the event `liftoff_acoustics` in `events.json`, with `oaspl_dB` per element and `sound_power_dB`. The vehicle report from `design_review` lists `vibration.liftoff_oaspl_dB` per element, the level the program office predicts at Earth liftoff.
- What the design chooses: a `QualificationLevels` part on any element above the first stage, with `acousticLevel` (overall sound pressure level in dB, 125 to 160) and `shockLevel` (peak shock response in g, 300 to 20,000). Left unset, the acoustic level is the element's predicted liftoff level plus 3 dB, and at least 137.4 dB (the Falcon 9 level plus the 6 dB qualification margin of its user's guide), and the shock level is 1,413 g (1,000 g plus 3 dB). Higher levels cost structure mass and lower ones save it.
- What reveals the hidden part: `vibration_test` on a payload element, with `acoustic_dB` (a 2 minute reverberant chamber test, 120 to 165) and `shock_g` (a pyroshock test, 100 to 30,000). It says whether a unit built to the element's levels survives. The levels it really survives lie 0 to 8 dB above its qualification levels.

### Pogo

The first longitudinal mode of the stack can couple with the propellant feed lines of a pump fed stage, and the oscillation grows until the stack breaks (Lewis, NASA TN D-5394, judged by NASA SP-8055 with 1% structural damping and a 6 dB margin). The feedback gain is fitted to the Saturn V analysis of NASA TN D-5714, in which the AS-502 first stage was about 5 dB unstable at 5 Hz at 120 s of flight. A second, local mode is active too: a center engine on a crossbeam, as in the S-II "mini pogo" of AS-503 to AS-508 at 16 to 18 Hz, which broke the crossbeam on Apollo 13. A stage has a center engine on a crossbeam when its cluster has an odd number of engines, five or more. Both modes are checked through every powered phase as propellant burns, because the frequencies change as the tanks empty and the acceleration rises.

- What the flight software can do: the feedback gain grows with the number of engines burning and their throttle, so a lower throttle late in a burn lowers it.
- What the design chooses: a `PogoAccumulator` part on a stage, with `oxidizerGasVolume`, `fuelGasVolume` or both, one accumulator per engine and line. They detune the feed lines from the structure. The vehicle report lists them as `pogo_accumulator_L_per_engine`.
- What reveals it: the pogo margins come from the program office's stage and engine model, which you do not see, so `design_review` does not print them. A `stage_static_fire` flies the real stage. Size the accumulators with your own model of the feed lines.

### Fins

The library has a `FinSet` part for the bottom of a stage (`finCount`, `span`, `rootChord`, `tipChord`, `sweep`). In 6-DOF the program office gives fins Barrowman's normal force at their center of pressure, with his fin and body interference factor. Fins move the center of pressure aft and make a stack more stable in pitch and yaw. Check `design_review` for whether your `FinSet` reached the compiled design: if it is not listed, it gives no force.

### Parachute canopy mass

A parachute's canopy and mortar mass is fabric over the nominal area plus the strength members for its rated load, fitted to the Apollo main and the MSL disk gap band parachute (the Orion main agrees within 15%). So a larger `diameter` and a higher design dynamic pressure both cost mass. Nothing about this is hidden; it is in the element's mass breakdown.

### Mass growth from design to flight

Built hardware is heavier than its design, as on every real program. Each part grows by a hidden factor, fixed for its design under the program secret, the same in every flight, and drawn again when the design changes. Propellant, pressurant and ballast do not grow.

- Parts whose mass comes from the program office's models fitted to flown hardware (stages, engines, shells, parachutes) grow around zero, with a spread equal to that model's own error against flown hardware: a few percent for stages and shells, more for engines you develop, about 15% for parachutes. Catalog engines and RTGs are existing hardware and grow least.
- Masses you enter yourself (a capsule's structure and extra mass) grow by the AIAA mass growth allowance (ANSI/AIAA R-020A-1999, kept by S-120-2006 and S-120A-2015). A team design is a concept at the "estimated" maturity, and the allowance for that maturity, 18% for structure and propulsion, covers about 9 parts in 10. The median growth is the smaller "layout" allowance.
- What the flight software observes: `mass` in `obs` is the estimated total mass of the vehicle as built, so it includes the growth.
- What reveals it: the `weighing` test builds one element as it sits in a given stack and weighs it, to 0.1% one sigma. Until an element is weighed, only its design mass is known. How much margin to carry (none, the full allowance, or weigh first) is the team's choice.
## Dynamics and navigation realism (active in every flight)

Every model in this section is on in the verifier. Your flight software never sees the true state of the vehicle. It sees what the vehicle's sensors and its navigation filter report, and it must be built for their errors. "6-DOF mode" above tells how the rigid body, the controller and the attitude sensors work; this section adds what is active on top of it. All numbers here are published sensor and model data. The draws of one world (a gyro's bias in that flight, the wind that day) stay hidden. In the nominal world every sensor error is zero.

### The complete `obs`

These are all the keys `obs` can hold. Keys marked "new" are not in the table under "The flight software".

| Key | When present | Units and meaning |
|---|---|---|
| `t`, `met` | always | s: TDB seconds since J2000, and seconds since launch |
| `mode`, `body` | always | `pad`, `flight` or `landed`; the central body (`Earth`, `Sun`, `Mars`) |
| `events`, `command_errors` | always | lists of strings since the last call |
| `milestones` (new) | always | the milestone names reached so far |
| `r`, `v` | always | m, m/s: navigated position and velocity, J2000, relative to the central body |
| `nav_sigma` (new) | in flight | `{"position_m", "velocity_mps"}`: the filter's own one sigma error, summed over the three axes (root sum square) |
| `nav_update` (new) | in flight | `{"kind", "t"}`: the filter's last measurement, `start`, `gps`, `dsn` or `trn`, and its time |
| `lat`, `lon`, `alt`, `height_above_surface` | near a planet | deg, deg, m, m |
| `sun_distance_au` (new) | in solar orbit | AU: navigated distance from the Sun |
| `airspeed`, `airspeed_vector`, `mach`, `q` | in the atmosphere | m/s, m/s (J2000), none, Pa |
| `surface` | on the ground | `{"lat", "lon", "alt"}` of the landing site |
| `attitude`, `attitude_q` | always | unit vector of the body x axis (J2000); quaternion `[w, x, y, z]`, body to J2000, as estimated |
| `body_rates_deg_s` | always | deg/s, body axes, as the gyros read them |
| `bank_deg` | always | deg |
| `angle_of_attack_deg` (new) | heatshield or capsule leading, in the air | deg: the estimated angle between the heatshield axis and the airflow |
| `mass_properties` | always | `{"center_of_mass_from_bottom_m", "inertia_kg_m2": [Ix, Iy, Iz]}` |
| `dynamics` (new) | always | `6dof` |
| `sensed_accel` | after the first arc | m/s², J2000: the accelerometers' mean specific force |
| `mass` | always | kg: estimated total mass |
| `elements` | always | per element: `id`, `kind`; a stage adds `fuel_kg`, `ox_kg`, `battery_Wh` and `engines` (`engine`, `count`, `starts_left`, `lit`, `out`); `rcs_kg` where there is RCS; a parachute adds `deployed`, `inflated`, `failed` |
| `samples_loaded` | always | true or false |
| `comms` | always | see "Comms" in the table under "The flight software" |
| `launch_azimuth_deg` | after range safety measures it | deg |
| `sample_temperature_C` (new) | once samples are loaded | °C |
| `faults` (new) | after a fault is detected | the faults the vehicle's fault detection has seen (see "What can go wrong") |
| `radar_valid` (new) | Mars descent, landing radar in use | false while the landing radar has failed |
| `nav_valid`, `imu_saturated`, `touchdown_sensors` (new) | only after the matching latent flaw shows itself | see "What can go wrong" |

### Navigation filter

**What happens.** The navigated state comes from a navigation filter, simulated by its error and its covariance. Between measurements the error is carried along the vehicle's orbit, so it grows the way a real orbit determination error grows. Each measurement is weighed against the prediction (a Kalman update), and the error shrinks.

**Published measurements.**
- GPS, at Earth only. Below 2,000 km altitude a fix is 5 m and 0.05 m/s per axis; from 2,000 to 20,000 km it is 10 m and 0.01 m/s. Above that there is no GPS.
- DSN tracking passes (two-way range, Doppler and delta-DOR), each a measurement of the whole state. In solar orbit one pass a day, 3 km and 3 mm/s per axis. Inside Earth's or Mars's sphere of influence one pass every 6 hours, 200 m and 1 mm/s per axis. A pass is taken only when the vehicle is in real two-way contact with a booked DSN complex (see `comms`), so passes are lost behind a planet, during solar conjunction and when no complex sees the vehicle. There are no passes in the last 6 hours before an atmosphere entry, as for MSL.
- On the ground (`pad`, `landed`) the position is known to 30 m. The filter is off while the vehicle stands on the ground, and it starts again at liftoff from that knowledge: 30 m and 0.01 m/s per axis, or the GPS fix at Earth. So a Mars ascent starts from the site, however long the vehicle waited there.
- Small forces nobody models exactly (outgassing after launch, uneven heat radiation, valve seepage) are part of the filter's uncertainty in cruise.

**What the flight software observes.** `r` and `v` with the filter's error; `nav_sigma` for how good the filter thinks it is; `nav_update` for the last fix. A Mars entry is flown on the state from the last pass before the 6 hour cutoff.

**What it can command.** Nothing direct. Plan cruise corrections where DSN contact gives fresh passes (`comms.dsn_contact`). Do not trust a state whose `nav_update.t` is old.

**What the design can choose.** The telecom design (see "Comms") sets how often the vehicle is in contact.

**Program office test.** `nav_end_to_end` checks the ground to flight navigation chain for a latent units error (Mars Climate Orbiter), see "What can go wrong".

### Landing navigation: radar altimeter and terrain-relative navigation

**What happens.** Below 20 km above the ground in flight, `alt` and `height_above_surface` come from a radar altimeter with a one sigma error of 1% of the height. Terrain-relative navigation (TRN) fixes the position once, at the first call between 4,200 m and 500 m above the ground, after the heatshield is gone. The camera matches the ground to its map and the position error drops to 13 m per axis. The velocity error stays.

**What the flight software observes.** The event `trn_fix`; `nav_update.kind` becomes `trn`. `radar_valid` false when a landing radar has failed.

**What the design can choose.** A `TerrainRelativeNavigation` part on the lander (15 kg, procured). `Redundancy.landingRadars` (1 or 2).

### Accelerometers and attitude sensors

**Published specs.** Accelerometers: the LN-200S, one sigma per axis bias 300 µg, scale factor 300 ppm, noise 35 µg/√Hz, axis misalignment 0.1 mrad, saturation at 40 g. `sensed_accel` is the mean over the last second. Gyros: bias 1 deg/h and angle random walk 0.07 deg/√h per axis (LN-200). Star tracker: two heads, about 5 arcsec per axis, up to 3 deg/s, blind inside the atmosphere. `mass` carries a one sigma error of 0.3%.

**What the design can choose.** Nothing: every vehicle carries this suite.

### Air data

**What happens.** `airspeed` and `airspeed_vector` are the navigated velocity relative to the air: the navigated velocity, less the planet's rotation, less the wind at the navigated position. So the flight software does see the wind, through its air data. `mach` and `q` come from the navigated state and the onboard atmosphere (see "6-DOF mode").

### Actuators from flight data

**What happens.**
- The shortest RCS pulse delivers the MR-104H data sheet's 8.23 N s, the impulse of 17.3 ms at full thrust (the valve opens and the chamber pressure rises and falls).
- After a shutdown or a burnout the main engine thrust falls with a time constant of 0.21 s, Apollo 11's S-II cutoff impulse.
- A cluster of 5 or more engines with an odd count gimbals all engines but the center one, as on the Saturn V's S-IC and S-II.

**What the design can choose.** `gimbalRange` of each engine; the engine count; `differentialThrottle` on a stage; `ReactionControlSystem.totalThrust` and `propellantLoad`.

### Coast disturbance torques

**What happens.** In a coast held in the deadband, the RCS must also cancel the torques of gravity gradient (3μ/r⁵ times r × I r, largest for a long stack close to a planet), solar radiation pressure (4.56 µN/m² at 1 AU on the side of the stack, reflectivity 0.3, center of pressure at mid length) and propellant venting. A torque that keeps one sign over the hold costs its angular impulse over the RCS lever arm and the exhaust speed. A torque that swings both ways about the target averages out.

**What the flight software observes.** `rcs_kg` falls faster in long holds near a planet. A wider `deadband_deg` does not remove this cost.

**What it can command.** `"attitude": {"rate_deg_s": [0, 0, 0]}` lets the vehicle drift instead of holding.

**What the design can choose.** The RCS propellant load and the stack's length and inertia.

### 6-DOF entry and parachute

**What happens.** An unpowered entry with a heatshield or capsule leading is a rigid body arc, not a trimmed point mass.
- The forces and the moment come from modified Newtonian theory on the sphere-cone and its afterbody, corrected to flight data by Mach number and bridged to free molecular flow in thin air.
- Pitch damping is from MSL's (70 deg cones) or Genesis's (60 deg cones) ballistic range tests. A capsule is dynamically unstable at small angles below about Mach 3, more so as it slows.
- The trim ballast sits off the axis, so the capsule trims at an angle of attack and its lift points to that side. A center of mass off the axis also turns a vehicle under thrust, and the autopilot trims the gimbals for it.
- The RCS holds the commanded `bank_deg` and pays propellant for it, at up to 20 deg/s of roll. It also damps pitch and yaw rates above 2 deg/s. In entry arcs the RCS torque is continuous (no shortest pulse).
- A deployed parachute is its own body, joined to the top of its carrier by a riser that pulls only when taut, so the vehicle swings under it.

**What the flight software observes.** `angle_of_attack_deg` and `bank_deg` as estimated, `body_rates_deg_s`, `rcs_kg`.

**What it can command.** `bank_deg` with `mode: "entry"`; `jettison_ballast:ID`; `deploy:ID`. Leave enough RCS propellant for the bank holds of the whole entry.

**What the design can choose.** `halfAngle`, `noseRadius`, `baseDiameter` and `trimAngleOfAttack` of the `Heatshield` or `EarthEntryCapsule`; the parachute.

**Program office test.** `wind_tunnel` measures a shell's drag and its lift-to-drag at trim. `mass_properties` weighs an element.

### Drag above the atmosphere (orbit decay)

**What happens.** Between the atmosphere interface (150 km at Earth, 130 km at Mars) and 1,000 km, every coast feels drag, so a low parking orbit decays over days to years. At Mars the density is Mars-DTM's, by height, latitude, local time, season and solar flux; at Earth it is the world's Earth atmosphere with its space weather. The drag coefficient is 2.2. A vehicle that holds an attitude shows the projected area of a cylinder of its widest diameter and total length, plus half the area of its solar arrays. A vehicle without attitude control tumbles and shows a quarter of its surface.

**What the flight software observes.** Only its navigated orbit. Plan parking orbits high enough for their wait.

### Commands, complete list

`engines`, `rcs` (`element`, `throttle`), `settle`, `dir`, `bank_deg`, `mode` (`entry`, or `thrust` to fly a blunt body as a thrusting vehicle), `attitude` (`rate_deg_s`, `torque_Nm`, `bandwidth`, `deadband_deg`), `next`, `wake`, and `actions`. The action verbs are `separate:ID`, `deploy:ID`, `jettison_ballast:ID`, `isru:ID`, `end:mission`, and the fault responses `switch_string`, `isolate_rcs`, `isolate_pressurant` and `restart` (see "What can go wrong"). An unknown verb is a command error.

The mission flies one vehicle. There is no Mars orbit rendezvous, no relative navigation sensor and no capture command.
## The environment and its data (active in every flight)

Every flight in the verifier flies through the same set of environment models. Each one is built from public data. The table names them. The sections below say what each does, how much it varies, what your flight software sees of it and what your design can choose. The per-world draws (which weather year, which Mars year, which space weather days) are never shown to the team or to the flight software.

| Provider | What it gives | Source |
|---|---|---|
| NRLMSIS 2.1 | Earth's air density, pressure and temperature from the ground to the exosphere | Emmert et al. 2021 and 2022, US Naval Research Laboratory, through `pymsis` |
| CelesTrak space weather | Daily solar flux F10.7 and 3-hourly geomagnetic index ap for the Earth atmosphere | CelesTrak `SW-All.csv` (GFZ Potsdam, DRAO Penticton, NOAA SWPC forecast) |
| Earth jet wind profile | Earth winds: one jet stream near 12 km plus a light wind near the ground | The flight-to-flight spread under "Dispersions" |
| Mars atmosphere table | Mars density from a CO₂ profile, the seasonal pressure cycle and the dust | Viking Lander 1 pressure cycle; six flown entries for the scatter |
| Montabone dust maps | Mars column dust optical depth by place and season, from real Mars years 24 to 36 | Montabone et al. 2015 and 2020, CC BY-SA 3.0 |
| Mars wind profile | Mars winds near Jezero | The flight-to-flight spread under "Dispersions" |
| JPL DE440 | Positions and masses of the Sun, planets and Moon | Park et al. 2021, JPL/NAIF |
| JGMRO_120F | Mars gravity field, flown to degree and order 30 | Konopliv et al. 2020, NASA PDS |
| Earth gravity | Tudat's Earth field, flown to degree and order 12 | Tudat |
| Jezero terrain | Ground height, roughness and rock cover at 20 m cells | USGS Mars 2020 and Mars Sample Return HiRISE and CTX terrain maps |
| Pad weather bank | Which launch weather rules were broken, every hour of 2005 to 2024, within 10 nmi of the pad | Public airport METARs; NASA KSC weather launch commit criteria |
| Range data | Launch azimuth limits per pad, and where land is | FAA spaceport data; Natural Earth land; SSCMAN 91-710 |

### Earth atmosphere and space weather

**What happens.** The density, pressure and temperature of the air at Earth come from NRLMSIS 2.1, the newer model that replaced NRLMSISE-00. Below about 100 km it follows measured temperature profiles. Higher up, the density depends on the solar flux and on geomagnetic storms, so the same orbit decays at different rates on different days.

**Published range.** Each world gets its own daily space weather for the mission years. The 81-day mean solar flux follows the NOAA forecast for the date. The day-to-day flux and the geomagnetic index are copied from real 27-day stretches of the observed record (1957 to 2026) at a similar level of solar activity, so storms and quiet spells keep their real size. On top of that the whole density has a flight-to-flight factor (see "Dispersions").

**What the flight software sees.** `q` and `mach` in `obs` while in the air. The space weather of the world is not given. Drag on a low orbit shows up in `sensed_accel` and in the navigated `r` and `v`.

**What it can command and choose.** Nothing changes the air. Plan for the spread: a parking orbit that is too low decays faster in a solar storm (see "Above the atmosphere interface" in "Time and frames").

### Earth winds

**What happens.** The launch stack climbs through a jet stream near 12 km and a light wind near the ground. The wind sets the angle of attack, the side force and the bending load at max q (see "Loads" under "What can go wrong"). The wind dies out above 60 km.

**Published range.** Jet stream 30 ± 15 m/s from a random direction, as listed under "Dispersions".

**What the flight software sees.** `airspeed_vector` and `airspeed` are the navigated velocity relative to the air, with the world's wind taken out. `q` and `mach` come from the true airflow in 3-DOF and from the onboard estimate in 6-DOF (see "6-DOF mode"). `sensed_accel` includes the side force.

**What the design can choose.** Structural margin on the tanks (wall thickness, stiffeners, factor of safety) against the bending of q·α.

### Mars atmosphere, dust and winds

**What happens.** Mars air is mostly CO₂. Its density follows a mean temperature profile, the seasonal pressure cycle (the polar caps freeze out about a quarter of the atmosphere and give it back), and the dust. Dust warms the middle atmosphere, which expands: in a dusty season the air is thinner near the ground and thicker high up, so an entry slows down higher. Dust also cuts the sunlight on the surface.

**Published range.** The dust comes from the observed dust of one real Mars year, picked per world from years 24 to 36 (1998 to 2023). The time of the flight maps to that year by the season (solar longitude Ls). So storms come in their real season, Ls 180 to 330, and three of the thirteen years had a global storm. Jezero's optical depth is usually about 0.5, and 0.6 to 1 in the dusty season. The density spread by height, the effect of dust by height and the rare arrival storm are given under "Dispersions". Mars winds near Jezero are 10 ± 8 m/s from a random direction: full speed from about 2 km above the ground, 30% of it at the ground, nothing above 40 km. One world in ten has high winds at the landing (see "Dispersions").

**What the flight software sees.** In the air: `q`, `mach`, `airspeed`, `airspeed_vector` (navigated, wind taken out), `sensed_accel`. On the surface the dust shows only through the power it cuts (see the power sections). The Mars year and the dust of the world are not given.

**What it can command.** The bank angle in entry (`bank_deg`, `mode: "entry"`) and when to deploy the parachute (`deploy:<ID>`), so it can steer around a thick or thin atmosphere by reading `sensed_accel`.

**What the design can choose.** Heat shield, L/D, parachute, solar arrays, battery and RTG, all with margin for the dusty season.

**Which test reveals more.** None measures the atmosphere. The `wind_tunnel` test measures the shell's drag and L/D that the atmosphere acts on.

### Planet positions and gravity

**What happens.** Around the Sun the vehicle feels the Sun, every planet and the Moon at their JPL DE440 positions. Around Mars it feels Mars's real lumpy gravity field (JGMRO_120F to degree and order 30), which moves a low orbit by kilometers over a day compared with a round planet. Around Earth it feels Earth's field to degree and order 12, plus the Moon, Sun, Venus and Mars.

**Published range.** None: these do not vary between worlds.

**What the flight software sees.** The navigated `r` and `v` of the central body (`body`), with the errors listed under `obs`. Your own copy of DE440 and of a Mars gravity field in `flight/` lets it predict the orbit as the simulator flies it.

### Jezero terrain

**What happens.** The ground under the lander sets the height above the surface, the radar altimeter reading and where touchdown happens. At touchdown the slope at the lander's scale and the tallest rock under it decide whether it stays upright (see "Mars touchdown" under "What can go wrong").

**Data.** The simulator lands on a 20 m map of about 32 by 30 km around Jezero, from the USGS Mars 2020 and Mars Sample Return terrain-relative navigation maps (HiRISE at 1 m, CTX at 20 m where HiRISE does not reach). Each cell has its height, its roughness and its rock cover, as described under "Terrain and landing". The same spot always has the same slope and rocks, in every world.

**What the flight software sees.** `height_above_surface`, the radar `alt` below 20 km above the ground, `trn_fix` events, `touchdown` and `surface`.

**What it can command.** Where to put down, by steering the descent with `dir` and `engines`.

### Launch weather

**What happens.** A launch goes only when the weather at the pad allows it. The weather is not modeled but replayed: each flight lives through one of 20 real years (2005 to 2024) of hourly reports from the airport near the pad, from its launch date on. An attempt is no-go in an hour that breaks a launch weather rule (lightning, cumulus, disturbed weather, thick cloud) or whose ground wind at 60 ft (18.3 m) is above the limit. On top of that, each attempt has a fixed chance of being no-go for reasons the airport reports cannot see (field mill readings, anvils, cumulus aloft, upper-level winds).

**The rules every design flies under.** The pad is Kennedy LC-39A. The launch window is 1 hour from the planned time (the opening hour and the next one are checked). A scrub recycles 24 hours. The launch period is 21 days from `launchEpoch`. If no attempt is go before it ends, the mission is lost to weather.

**Published range.** At Kennedy, the replayed reports alone give about 11% no-go at the planned time. With the unseen weather added, the all-weather no-go matches the 45th Weather Squadron's record of 24% of countdowns (18% scrubbed and 6% delayed for weather, 1988 to 2017; Winters 2017). Summer afternoons are worst, from thunderstorms. Which year a world replays is never shown.

**What the flight software sees.** When it first commands thrust on the pad, the countdown runs. Each scrub appears in `events` as `launch_scrubbed`. The hold appears in `command_errors` as "launch held on the pad by weather: next attempt in N h (M scrubs)", and the engines are not started. `t` then moves on to the go time, so the departure happens later than planned and the flight software must plan its Earth departure from the real liftoff time, not from `launchEpoch`. A launch period that closes ends the flight with that reason.

**What it can command.** Command ignition again after a hold. It cannot launch through a no-go hour.

**What the design can choose.** `launchEpoch` (when the period opens) and `groundWindLimit` on the `MissionStack` (m/s, more than 0 and at most 40; the default is 17.5 m/s, the 34 kt direction-free upper limit of the KSC criteria). A launch date with room for scrubs inside the departure window costs nothing but delta-v.

### Range safety

**What happens.** The Eastern Range must approve the trajectory. Three checks run in Earth ascent:
- the launch azimuth, measured once when the vehicle passes 5 km, must be within the pad's limits: 37 to 114 deg for LC-39A;
- in powered flight the instantaneous impact point (where the vehicle would land in vacuum if thrust stopped now) must not fall on land between 10 and 2,000 km from the pad;
- at each separation in Earth ascent, the spent stage's impact point must not fall on land in that zone either.

Any failed check ends the flight. Land is the Natural Earth 1:10m map at 0.05 deg. Beyond 2,000 km the impact point is not checked, as the range accepts the African overflight of Mars launches.

**What the flight software sees.** `launch_azimuth_deg` in `obs`, once it has been measured above 5 km (null before).

**What it can command.** The ascent steering (`dir`) that sets the azimuth, and when to separate stages (`separate:<ID>`).

**What the design can choose.** Stage sizes and burn times, which set where spent stages fall.
## Power, thermal, comms and samples realism (active in every flight)

These models run in every verifier flight. Each part below says what happens, the published size of the effect,
what your flight software sees, what it can command, what your design can choose, and which program office test
tells you more. The flight software never sees the simulator; this section and the parts named under "The design"
are all it gets.

### Power sources

What happens. The power balance runs on every coast, burn and sol on the surface.
- An `MMRTG` loses power over the years as Curiosity's did: plutonium decay plus thermocouple ageing that slows
  down with time. NASA gives about 4.8% a year as representative for the first years (Zakrajsek et al. 2016).
  Each unit gives its 110 W at launch.
- A `SolarArray` in space gets the sunlight at its distance from the Sun. In a coast the arrays track the Sun. In
  a burn the vehicle points its thrust, and the arrays see the cosine of the angle between the thrust axis and
  the Sun, or nothing when the Sun is behind them. Arrays also lose a small fraction a year to ageing.
- A `SolarArray` on Mars gets the sunlight of the landing site's latitude, season and hour. Dust in the air cuts it,
  and dust settles on the arrays a little every sol and faster in a storm. InSight and Opportunity are the
  published cases: InSight's daily energy fell to a small fraction of its first sol over four years, and
  Opportunity's 2018 storm (optical depth up to about 11) ended its mission. The dust in each world follows the
  Montabone dust maps (see "Dispersions").
- A `FissionPowerUnit` gives a steady 10 kWe.
- The base avionics load, the radio's transmit power (see "Comms"), survival heaters, tank heaters, cryocoolers
  and the ISRU plant all draw from the same bus.

Batteries. A `Battery` carries the night on Mars and every shadow in space.
- Only 60% of its capacity may be used (the depth of discharge of InSight's and MER's flight rules).
- A charge and discharge cycle returns 90% of the energy.
- Capacity fades with the time since the cells were made, which is about a year before launch, and with the
  cycles it runs. Mars Express's and MER's flown batteries lost a few percent to about 20% over many years.
- Shadows: every coast outside the atmosphere around Earth or Mars is checked for the shadow of the planet
  (umbra and penumbra). In the longest shadow of each arc, the battery must carry the load less any RTG power.
  A low Earth parking orbit has shadows of about 35 minutes. If the battery cannot carry the longest shadow,
  the flight fails with a message that gives the shadow length and the energy it needed.
- On Mars the battery must carry the hours the arrays do not cover, and the tank heaters peak before dawn, when
  it is coldest.
- Running short of power for more than 3 days in a row still loses the vehicle (see "What can go wrong").

What the flight software sees. `elements[].battery_Wh` on each stage is the battery's installed capacity, less
any string lost to a failure. There is no state of charge or power reading in `obs`: plan the power balance in
your own models. A lost battery string shows up in `faults` (see "What can go wrong").

What it can command. Nothing directly. Pointing (`dir`) during long burns changes what body fixed arrays get.

What the design chooses. `MMRTG` count (at most 3 per element), `SolarArray` `area`, `Battery` `energy`,
`FissionPowerUnit` count, and `Redundancy` `batteryStrings` (1 to 4).

Which test tells you more. None: the power sources follow their published flight data and have no hidden factor.
Battery string failures are part of "Reliability".

### Thermal: survival heaters, tank heaters and boil off

What happens.
- In space, any element with power hardware, or the capsule, runs survival heaters. Their power falls as the
  sunlight on the craft rises and grows with the time in shadow, scaled from Mars Express's measured heater
  power (about 80 to 190 W a day over three Mars years) by the craft's size.
- On Mars, tank heaters keep storable propellants 10 K above freezing against the Jezero sky and ground,
  hour by hour and season by season, as Perseverance's MEDA sensor measured it: about 170 to 178 K before dawn
  and 229 to 244 K early in the afternoon. Each world shifts it by its own mean surface temperature.
- Cryogens boil off at the heat leak less what the cryocoolers lift (see "What can go wrong").

What the flight software sees. The propellant masses in `elements` (`fuel_kg`, `ox_kg`) as gauged (see the
propellant gauging section), so boil off shows up as falling propellant.

What the design chooses. `MultiLayerInsulation` (`layers`), `SprayOnFoamInsulation`, `SurfaceThermalBlanket`
(`heatLeakCoefficient`), and `Cryocooler` (`inputPower`).

Which test tells you more. None: there is no hidden thermal factor.

### ISRU oxygen

What happens. The `IsruOxygenPlant` follows MOXIE on Perseverance (16 runs, 122 g of oxygen in all). It draws
about half of its rated power just to keep its 800 C stack hot before any oxygen comes. A cold start takes about
two hours of heating. Above that, oxygen comes in proportion to the extra power. The intake follows the air
density, so a thin season makes less, and liquefying the oxygen uses the cryocooler model. The plant only draws
what the power to spare and the night battery allow. When power runs short it goes cold and must warm up again.
MOXIE's published peak is about 12 g an hour at about 320 W. So the "0.75 kg per sol per kW" of the design rule
is only a first guess.

What the flight software sees. The oxygen tank filling in `elements[].ox_kg`. An ISRU unit failure shows in
`faults`.

What it can command. The action `isru:ID` starts the plant on the stage `ID`, which needs a LOX tank.

What the design chooses. `IsruOxygenPlant` `inputPower`, `Redundancy` `isruUnits` (1 or 2), enough power and
battery for the plant through the night.

Which test tells you more. `isru_demo` measures the plant's oxygen output in kg per sol at its rated power. The
as built plant has its own hidden output factor (see "The hidden truth").

### Comms and the Deep Space Network

What happens.
- The DSN's three complexes see the vehicle when it is above their horizon, for the number of passes per day you
  book. The link closes at the rate your radio, dish and range allow.
- Solar conjunction: below 2 deg Sun Earth probe angle no commands go up (the moratorium).
- A correction burn in solar orbit is a ground planned burn. It is refused, as a command error, during the
  moratorium or when there was no uplink in the 7 days before it.
- Mars entry, descent and landing must send data home, as NASA has required since Mars Polar Lander: direct to
  Earth when Earth and a DSN complex can see the vehicle and the Sun Earth probe angle is above 2.3 deg, or by
  UHF relay to an orbiter (the plasma of peak heating blacks out UHF for 25 to 100 s). An entry with no
  coverage reports `entry_data_lost`. It does not fail the flight.

What the flight software sees. `comms` in `obs` (see "The flight software"): `dsn_contact`, `downlink`, `sep_deg`,
`owlt_s` (s), `moratorium` and `downlink_bps` (bits per second). At Mars touchdown the event `edl_data` (or
`entry_data_lost`) gives `dte_fraction`, `relay_fraction`, `covered_fraction` and `uhf_blackout_s`.

What it can command. Nothing about the link. Time cruise corrections for a recent uplink and outside the
moratorium.

What the design chooses. `TelecomSystem` (`band`, `highGainAntennaDiameter`, `transmitPower`, `gimbal`,
`uhfRelay`) on each element, and `dsnPassesPerDay` (1 to 3) on the `MissionStack`. A bigger radio adds mass and
transmit power to the bus.

Which test tells you more. None: the link follows the public ephemerides and your design.

### Storage ageing of propulsion

What happens. Valves, seals and regulators can fail during long storage in space. Each stage that carries
propellant has a small constant chance of such a failure from launch, at the fatal rate of chemical propulsion on
operating spacecraft (Saleh et al. 2017, about 0.1% to 0.2% a year). A stage whose propulsion has aged out fails at
its next engine start. Over a 2.6 year mission it is about 0.3% per stage.

What the flight software sees. The engine start fails (`ignition_failed:ID` in `events`).

Which test tells you more. None. Fewer stages that must restart after a long cruise means less of this risk.

### Samples: mass, temperature and planetary protection

Sample mass. Each `sampleContainers` container is a loaded Orbiting Sample of 30 tubes, about 11 kg (NASA's
Orbiting Sample specification).

Sample temperature. MSR keeps the sample tubes below minus 20 C, with short transients up to plus 30 C allowed.
Above plus 30 C the samples are lost to science, and the flight fails with a message giving the temperature and
the phase.
- On Mars, the samples settle towards the site's surface temperature once loaded.
- The Mars ascent gives MSR's expected plus 30 C transient: at the limit, not over it.
- In space, they settle towards the temperature of a sphere in sunlight at the Sun distance, set by the carrier's
  finish (absorptance over emittance). The default finish puts them near minus 40 C at 1 AU, as MSR expected.
- At Earth entry heat soaks back from the heat shield. MSR expected minus 40 C to at most plus 25 C. The rise grows
  with the capsule's heat load against its TPS capacity, so a thin TPS layer close to its limit heats the samples.

Planetary protection. After Mars liftoff with the samples aboard:
- Earth avoidance: the trajectory must miss Earth (closest approach above 120 km) until 7 days before closest
  approach, as the ESA Earth Return Orbiter planned. An earlier Earth intercept fails the flight.
- Containment: at Earth touchdown the sample container must take less than 3,500 g, or containment is lost and
  the flight fails. Above 2,500 g the samples' science is lost (event `samples_science_lost`). A capsule that
  lands on a parachute must still hold containment if the parachute failed, at its own terminal speed. So a
  parachute does not excuse a weak capsule.

What the flight software sees. `samples_loaded`, and `sample_temperature_C` (C) once the samples are loaded.

What it can command. Its trajectory: plan the Earth return so that the last week brings the intercept, and keep
the Earth entry heat load within the TPS layer's capacity.

What the design chooses. `sampleCarrierAlphaOverEpsilon` on the `SampleContainmentAssembly` (0 to 1, default 0.5),
`sampleContainers`, the capsule's `landing` and the TPS layer.

Which test tells you more. `arcjet` (the heat load your TPS layer absorbs), which sets the soak back margin. The
sample temperature and containment rules have no hidden part.
## Failures, fault protection and the program office (active in every flight)

Parts now fail during the verifier's flights, and the flight software must answer them. This section lists every failure the flight can meet, what the flight software sees, what it can command, what the design can choose, and which program office test finds the hidden part. It adds to "Reliability", "What can go wrong" and "The program office and R&D tests".

Each failure is fixed by the world: a part fails at a time drawn from the world's seed. Two designs with the same parts meet the same failures in the same world, and a design with a spare survives the worlds where a single part dies. The drawn failure times never reach `obs`. The verifier draws fresh worlds that you never see.

### What the flight software sees and sends

- `obs["faults"]` is a list of every fault the vehicle's fault detection has seen so far. Each item has `element`, `kind` and `met_s` (mission time of the fault, s), and some kinds add a field: `string` (`A` or `B`), `branch`, `radar`, `capacity_Wh`, `isru_kW`, `burn_s`, `g`. Each new fault also appears as a `fault` event.
- Each element in `obs["elements"]` that has avionics carries `avionics`: `strings` (1 or 2), `cross_strapped`, `active` (`A`, `B` or `cross`), `healthy` (the strings with no failed unit), `safe_mode`, and `rebooted` after a radiation upset.
- `obs["radar_valid"]` during Mars descent below 20 km above the ground: false when no landing radar works.
- `obs["nav_valid"]`, `obs["imu_saturated"]` and `obs["touchdown_sensors"]` (three booleans on a Mars lander after its legs deploy) are described under "Flown failures" below.

The action verbs, each sent in the command's `actions` list as `verb:ELEMENT`:

| Verb | When | What it does |
|---|---|---|
| `switch_string:<id>` | an element is in safe mode after an avionics string failed, and a healthy string is left | turns on the other string (or the healthy units of both, if cross strapped) and leaves safe mode. Event `string_switched`. With nothing to switch to it is a command error. |
| `isolate_rcs:<id>` | an `rcs_leak` fault | closes the leaking branch. The leak stops. With two RCS branches the element keeps half its RCS thrust; with one it has no RCS left. Event `rcs_isolated` with the thrust left. |
| `restart:<id>` | an element with `avionics.rebooted` and `safe_mode` | brings the computer back after a radiation upset. Event `avionics_restarted`. Sent at any other time it is a command error. |
| `isolate_pressurant:<id>` | a `tank_pressure_rising` fault on a pressure fed stage | closes the pressurant line before the oxidizer tank ruptures. Event `pressurant_isolated`. |

An element in safe mode stops its engines at once and ignores every engine, RCS and steering command until it is switched or restarted. If every steering element is in safe mode, `dir` and `settle` are ignored too. Answer a fault in the same call that reports it: a stage in safe mode during a burn is coasting.

A small wrapper that answers these faults, `FaultProtection`, ships with the simulator's notes as an example of the pattern: each call it sends `switch_string` for an element in safe mode with a healthy string left, `restart` for a rebooted element, and `isolate_rcs` once per leak, then calls your own `step`. Write your own version in `flight/`; nothing outside `flight/` is on the flight software's path.

### Random part failures in flight

Rates come from flown fleets. Avionics, RCS valves, batteries and the ISRU plant fail on a Weibull law per unit, fitted by Castet and Saleh (2009) to 1,584 Earth orbiting spacecraft launched from 1990 to 2008. Every one of their shapes is below 1, which means infant mortality: the failure rate is highest early in a unit's life and falls with age. These fleet rates come from satellites that mostly fly redundant units already, so per string they are a low estimate.

| Part class | Law and published rate | Fault `kind` | What happens | Spare (design) |
|---|---|---|---|---|
| Avionics string (computer, inertial unit, radio) | Weibull per unit; a string fails at its first unit's failure. Flown case: MSL swapped from RCE-A to RCE-B on sol 200 after a flash memory fault. | `avionics_string` | The element goes to safe mode. With a single string the element can no longer be commanded and the flight ends. | `avionics`: `single`, `dual` (a cold spare string, a full copy of the avionics mass) or `crossStrapped` (dual, plus 6 kg; a unit failure removes only that unit, so the element is lost only when the same unit fails on both sides) |
| RCS leak (a thruster valve fails open) | Weibull, thruster and fuel units. Flown case: the Shuttle saw about 1.5 primary thruster leaks or fail offs per flight in the early 1990s. | `rcs_leak` | The element's RCS propellant drains at 0.1% of its RCS load per second until `isolate_rcs`. | `rcsBranches` 2 (plus 2 kg) keeps half the RCS thrust after isolation |
| Battery (a cell or string short) | Weibull, battery cell and electrical distribution | `battery_string` | One string's share of the capacity is lost; `capacity_Wh` gives what is left. | `batteryStrings` 1 to 4 (the battery carries 5% more for string switches) |
| ISRU plant | Weibull, from the sol the plant starts | `isru_unit` | One unit's share of the plant power is lost; `isru_kW` gives what is left. | `isruUnits` 2 (a second plant's mass) |
| Landing radar | per landing, at power on below 20 km in Mars descent. Every radar or lidar guided powered landing with a published sensor record: 4 sensor failures in 34 landings (Apollo, Surveyor, Chang'e, Chandrayaan-3, SLIM and others). | `landing_radar` (with `radar`) | With no working radar, `alt` and `height_above_surface` come from inertial navigation, not the radar, and `radar_valid` is false. | `landingRadars` 2 (plus 12 kg) |
| Separation (pyro event) | per event: 11 separation failures in 2,091 launches from 1980 to 1999 (Chang, Aerospace Corp), about 0.18% per event. About 45% of pyro system failures are in the initiation; the rest are in the mechanism (Bement, NASA). | `separation_failed` | The element stays attached. A failed separation stays failed. The event `separation_failed` gives `cause`: `initiator` or `mechanism`. | `pyroInitiators` 2 (plus 0.1 kg) covers the initiation share only |

These spares are set in a `Redundancy` part inside the element (see the library's `Redundancy` part def). Each spare adds its mass to the element's dry mass.

### Flown failures: latent flaws

A new vehicle can carry a build, integration or software error that destroyed a real mission. Whether your vehicle has one is a hidden draw. The prior chance of each class is the FAA's first flight loss rate for experienced developers (5 of 18 first flights lost, 1980 to 2002) times that class's share of launch failures in Chang's 1980 to 1999 record. A program office test finds a flaw with a coverage below 1, and each test of that kind lowers the chance that the flaw is still there. The test result gives that remaining chance.

| Flaw | Real case | What the flight software sees | Program office test |
|---|---|---|---|
| reversed polarity | Proton-M 2013, Vega VV17 2020 | a stage steers the wrong way while it burns | `phasing_test` |
| heritage software overflow | Ariane 5 flight 501, 1996 | above some horizontal speed in ascent, `nav_valid` goes false, `r` and `v` freeze, fault `inertial_reference_overflow`, and the nozzles go hard over | `trajectory_requalification` |
| weak strut | Falcon 9 CRS-7, 2015 | a pressurant bottle strut in an upper stage breaks as the axial g rises; fault `strut_failure`; the stage is lost | `part_lot_screening` |
| inverted deceleration switch | Genesis, 2004 | the capsule's sequencer never senses entry, so a `deploy` of its parachutes does nothing (event `parachute_not_fired`) | `centrifuge_test` |
| IMU saturation hold | Schiaparelli, 2016 | the IMU saturates for about 1 s at parachute inflation (`imu_saturated` true, fault `imu_saturated`), and the attitude error can make `height_above_surface` read negative | `edl_hil_test` |
| units mismatch | Mars Climate Orbiter, 1999 | the navigated position near Mars, before entry, is biased high | `nav_end_to_end` |
| radiation upset | Phobos-Grunt, 2011 | heavy ions reboot both strings in cruise; fault `avionics_reset`; the element is in safe mode with `rebooted` until `restart:<id>` | `radiation_test` |

One more is not a flaw but how real leg sensors behave, so every Mars lander has it:

- **Touchdown sensor transient** (Mars Polar Lander, 1999). When the legs deploy, at about 1,500 m above the ground once the heat shield is gone, event `legs_deployed`, one or more of the three `touchdown_sensors` reads true on that call only. Flight software that latches it cuts the engines high above the ground. Believe touchdown only near the ground and on two calls in a row. `touchdown_sensor_test` measures how many legs give the false signal and for how long.

Two flight software guards cover two of these cases whatever the tests say: ignore a negative `height_above_surface` while `mode` is `flight`, and ignore `touchdown_sensors` until the vehicle is close to the ground.

### Propulsion failures: latent flaws

Six more flaws come from the propulsion rows of the flown failure record, each with its own prior from the same FAA rate times the share of that cause in the record. They are being wired into the flight now: until the program office lists their test kinds in `program_info`, those tests cannot be ordered. Write the flight software's answers to them (`isolate_pressurant` above) now.

| Flaw | Real case | Applies to | What happens | Program office test |
|---|---|---|---|---|
| feed line leak | Falcon 9 Starlink 9-3, 2024 | a pump fed upper stage | it leaks oxidizer in its first burn (fault `ox_tank_pressure_low`); its restart fails (fault `engine_restart_failed`, and engine commands to it are ignored) | `feed_leak_test` |
| regulator stuck open | Peregrine, 2024 | a pressure fed stage | in the first 30 days of the first cruise, fault `tank_pressure_rising`. Unless the flight software sends `isolate_pressurant:<id>` in time, fault `oxidizer_tank_ruptured` and most of the oxidizer is lost. The time allowed is hidden: send it on the call that reports the fault. | `propulsion_checkout` |
| check valve migration | Mars Observer, 1993 | a pressure fed stage after a long cruise | the first start pressurizes the tanks, the line ruptures (fault `pressurization_line_ruptured`) and the stage is lost | `long_duration_prop_test` |
| check valve stuck | Akatsuki, 2010 | a pressure fed stage that burns for orbit insertion at Mars | the first burn at Mars ends early (fault `engine_shutdown_early` with `burn_s`), and the engine will not fire again for a day | `valve_qualification` |
| solid motor nozzle erosion | Vega C VV22, 2022 | a solid motor | the throat erodes through partway into the burn; the stage is lost | `srm_lot_static_fire` |
| solid motor case burst | Titan 34D-9, 1986 | a solid motor | the case burns through seconds after ignition; the stage is lost | `srm_lot_static_fire` |

A pressure fed design survives check valve migration if the element carries pyro isolation valves (`pyro_isolation_valves` on the element).

### Parachute failure and no-light checks

The verifier's backup parachute and retry after a no-light checks are on in every grade. It flies no engine-out checks: every engine failure counts as lost. See "Reliability" under "When an engine fails" and "When a parachute or an ignition fails".

### Booster recovery

A first stage (`LaunchStage`) may set `recovery` to `"none"` (the default), `"rtls"` (boostback, entry and landing burns back to the launch site) or `"droneship"` (entry and landing burns onto a ship downrange). A real recovered stage holds back propellant for its return burns and carries legs and grid fins, about 9% of its dry mass on Falcon 9. Falcon 9's commonly quoted reserves are 10% to 15% of the first stage load for a return to the launch site and about 4% to 6% for a drone ship. Plan that reserve yourself: do not burn it in the ascent. The field may be set only on the first stage. The return flight is not flown: the landing is a draw at the Falcon 9 record, about 99.4% in recent years. After the first stage separates, `obs["booster_recovery"]` gives `mode` and `landed`.

### Retropropulsion and plume effects

- **Supersonic retropropulsion.** Engines firing into a supersonic flow (Mach 1 and above) change a blunt body's drag as wind tunnel data show (Jarvinen and Adams 1970; Korzun 2012). One central nozzle pushes the bow shock off the body and the drag collapses as the thrust coefficient rises. Nozzles near the rim of an aeroshell keep most of the drag up to a thrust coefficient of about 1, then lose it. Nozzles under a stage, closer to the axis, lose almost all of it. So the number and placement of engines (`count` on the engine part) changes how much aerodynamic braking is left while they fire.
- **Plume surface interaction on Mars.** An engine firing near the ground digs a crater under the lander, more the longer and lower it fires. Pulsed engines dig faster than steady ones (Phoenix). A crater that reaches under a footpad puts that pad on a slope, and a slope above the landing limit tips the lander. A sky crane, or cutting the engines a metre or two above the ground, digs little; a long hover near the ground digs deep. MSL's own descent dug craters about 7 to 10 cm deep in loose soil.
- **Backshell heating.** The backshell is heated too, at a few percent (Earth) to about a tenth (Mars, from Mars 2020's MEDLI2 backshell sensors) of the heat shield's stagnation heating. A backshell's `tps` and areal mass must carry that heat load and peak flux, or it burns through. The heat load each backshell absorbed is reported after the flight.

### Program office tests for these failures

These tests are in `program_info` with their prices and facilities. Each reports a summary in words and a `measured` object.

| Test | Facility | Target | Reports |
|---|---|---|---|
| `avionics_burn_in` | thermal vacuum | an element with avionics; params `hours` 100 to 2000 | the units that failed and were replaced, and one string's chance to fail in 4 years after the burn in. Burn in ages the units, so the flight units start past their infant mortality. |
| `phasing_test` | stage stand | a stage | `flaw_found` and `remaining_chance` for reversed polarity |
| `trajectory_requalification` | avionics lab | the launch vehicle | the same, for the heritage software overflow |
| `part_lot_screening` | structures lab | a stage | the same, for a weak strut |
| `centrifuge_test` | structures lab | the return capsule | the same, for an inverted deceleration switch |
| `edl_hil_test` | avionics lab | the Mars lander | the same, for the IMU saturation hold |
| `nav_end_to_end` | avionics lab | the navigation chain | the same, for the units mismatch |
| `radiation_test` | thermal vacuum | the avionics parts list | the same, for memory parts that upset |
| `touchdown_sensor_test` | drop range | a lander stage with legs | how many of 3 leg sensors gave a spurious touchdown signal, and its length in ms |

A flaw that a test finds is fixed. A test that finds nothing still lowers the chance that the flaw is there.

### Investigations of a failed test

After a test fails, the agent who ordered it can call `investigate(test_id)`. It is free and returns synthetic telemetry from 10 s before the failure to 2 s after, each channel in percent of nominal with sensor noise, and the price of a root cause. `buy_root_cause(test_id)` pays that price from the R&D budget (a quarter of the failed test's cost, at least $0.5M) and names the cause class, whether it is a design flaw or a random failure, and what to change, in words. A design flaw, such as combustion instability or too little cooling, fails a retest of the same design the same way; change the design to get a new part key.
