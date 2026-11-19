package MarsSampleReturnReference {
    doc /*
     * The reference mission's vehicle (reference/vehicle.json), written in the design language.
     * Elements are named by the ids the flight software uses. The joints at the end list the launch
     * stack from the pad up.
     */
    private import ScalarValues::*;
    private import SI::*;
    private import MarsReturnUnits::*;
    private import MarsReturnLibrary::*;

    // ---------------------------------------------------------------- engines

    part def R2 :> ProcuredEngine { :>> designation = "Raptor 2"; }
    part def RV :> ProcuredEngine { :>> designation = "Raptor Vacuum"; }
    part def AJ :> ProcuredEngine { :>> designation = "AJ10-190"; }

    // ---------------------------------------------------------------- the vehicle on the pad

    part vehicle : MissionStack {
        :>> launchEpoch = "2030-12-28T00:00:00";

        part S1 : LaunchStage {
            doc /* Booster: one stainless steel tank body with a common bulkhead, eleven Raptor 2. */
            :>> outerDiameter = 11.2 [m];
            :>> propellantLoad = 1500000 [kg];
            part fuelTank : PropellantTank {
                :>> propellant = "LCH4";
                :>> material = "AISI 301 full hard";
                :>> radius = 5.6 [m];
                // walls of 3.6 mm by the load rule: q-alpha of 40 kPa x 10 deg at 2 g gives 0.38 of the wall's
                // SP-8007 strength with the 1.6 factor (3.2 mm: 0.56), against a hidden knockdown down to 0.85
                :>> barrelLength = 5.805 [m];
                :>> wallThickness = 3.6 [mm];
                part baffles : RingBaffles { :>> widthFraction = 0.15; :>> spacing = 1.0 [m]; }
            }
            part oxidizerTank : PropellantTank {
                :>> propellant = "LOX (MIL-PRF-25508)";
                :>> material = "AISI 301 full hard";
                :>> radius = 5.6 [m];
                :>> barrelLength = 7.740 [m];
                :>> wallThickness = 3.6 [mm];
                part baffles : RingBaffles { :>> widthFraction = 0.15; :>> spacing = 1.0 [m]; }
            }
            part bulkhead : CommonBulkhead;
            part pressurization : PressurizationSystem { :>> meop = 3.0 [bar]; }
            part mainEngines : R2[11];
            part pogo : PogoAccumulator {
                doc /* LOX pogo accumulator at each engine's pump inlet, as Saturn V's S-IC flew from AS-503 on. */
                :>> oxidizerGasVolume = 0.020 [m^3];
            }
            connection : PropellantFeed connect fuelTank to mainEngines;
            connection : PropellantFeed connect oxidizerTank to mainEngines;
            part acs : AttitudeControlSystem { :>> maxSlewRate = 5.0 [deg/s]; }
        }

        part S2 : LaunchStage {
            doc /* Upper stage: one Raptor Vacuum; a tank body of two domes with a common bulkhead, inside the 11.2 m body. */
            :>> outerDiameter = 11.2 [m];
            :>> propellantLoad = 300000 [kg];
            // loaded a little fuel rich: the TMI's propellant utilization runs the engine at the ratio left in the
            // tanks, so both reach the low level floor together (gnc/earth.py propellant_utilization)
            :>> loadedMixtureRatio = 3.55;
            part fuelTank : PropellantTank {
                :>> propellant = "LCH4";
                :>> material = "AISI 301 full hard";
                :>> radius = 5.009 [m];
                :>> barrelLength = 0.0 [m];
                :>> wallThickness = 2.85 [mm];
                part baffles : RingBaffles { :>> widthFraction = 0.15; :>> spacing = 1.0 [m]; }
            }
            part oxidizerTank : PropellantTank {
                :>> propellant = "LOX (MIL-PRF-25508)";
                :>> material = "AISI 301 full hard";
                :>> radius = 5.009 [m];
                :>> barrelLength = 0.0 [m];
                :>> wallThickness = 2.85 [mm];
                part baffles : RingBaffles { :>> widthFraction = 0.15; :>> spacing = 1.0 [m]; }
            }
            part bulkhead : CommonBulkhead;
            part pressurization : PressurizationSystem { :>> meop = 3.0 [bar]; }
            part mli : MultiLayerInsulation { :>> layers = 10; }
            part vacuumEngine : RV[1];
            connection : PropellantFeed connect fuelTank to vacuumEngine;
            connection : PropellantFeed connect oxidizerTank to vacuumEngine;
            part rcs : ReactionControlSystem {
                :>> propellantLoad = 1000.0 [kg];
                :>> totalThrust = 40000.0 [N];
            }
            part acs : AttitudeControlSystem { :>> maxSlewRate = 5.0 [deg/s]; }
        }

        part FAIR : PayloadFairing {
            :>> outerDiameter = 11.2 [m];
            :>> length = 23.0 [m];
        }

        part HS : Heatshield {
            doc /* Mars entry heatshield, a 70 deg sphere-cone trimmed by jettisonable ballast. TPS 4.6 kg/m^2
                 * (was 4.2): with the 1.3 t heavier surface payload one world (seed 1007) used the whole heat
                 * load capacity of 4.2 kg/m^2. */
            :>> baseDiameter = 11.0 [m];
            :>> halfAngle = 70.0 [deg];
            :>> noseRadius = 2.75 [m];
            :>> trimAngleOfAttack = 9.33 [deg];
            part tps : ThermalProtection {
                :>> material = "SLA-561V";
                :>> thickness = 17.69 [mm];
            }
        }

        part DL : Lander {
            doc /* Descent stage and lander: three RTGs, three AJ10-190. */
            :>> outerDiameter = 3.0 [m];
            :>> propellantLoad = 1450 [kg];
            part fuelTank : PropellantTank {
                :>> propellant = "MMH (MIL-PRF-27404)";
                :>> material = "IM7/8552";
                :>> layup = "[45/-45/0/90]2s";
                :>> radius = 0.602 [m];
                :>> barrelLength = 0.0 [m];
                :>> wallThickness = 5.23 [mm];
            }
            part oxidizerTank : PropellantTank {
                :>> propellant = "NTO (MIL-PRF-26539)";
                :>> material = "IM7/8552";
                :>> layup = "[45/-45/0/90]2s";
                :>> radius = 0.602 [m];
                :>> barrelLength = 0.0 [m];
                :>> wallThickness = 5.23 [mm];
            }
            part pressurization : PressurizationSystem { :>> meop = 12.8 [bar]; }
            part mli : MultiLayerInsulation { :>> layers = 20; }
            part blanket : SurfaceThermalBlanket { :>> heatLeakCoefficient = 0.05 [W/(m^2*K)]; }
            part descentEngines : AJ[3];
            connection : PropellantFeed connect fuelTank to descentEngines;
            connection : PropellantFeed connect oxidizerTank to descentEngines;
            part legs : LandingGear { :>> touchdownSpeedRating = 2.5 [m/s]; }
            part trn : TerrainRelativeNavigation;
            part rtgs : MMRTG[3];
            part battery : Battery { :>> energy = 0.25 [kW*h]; }
            part acs : AttitudeControlSystem { :>> maxSlewRate = 30.0 [deg/s]; }
            // Redundancy: dual-string avionics and a second landing radar, as vehicle.json's
            // "redundancy": {"avionics": "dual", "landing_radars": 2}. With one radar, a radar lost in entry left the
            // descent on the navigated height, about 20 m off at touchdown (seed 1000).
            part redundancy : Redundancy {
                :>> avionics = AvionicsRedundancy::dual;
                :>> landingRadars = 2;
            }
        }

        part SRF : SurfacePlatform {
            doc /* Surface sample handling platform with its own RTG. */
            :>> outerDiameter = 1.0 [m];
            part rtg : MMRTG[1];
            // Cross-strapped spare avionics (+66 kg): a single string lost on the way to Mars or on the surface ends
            // the mission (about 1.3% over the 2.1 years to the Mars liftoff, Castet and Saleh rates).
            part redundancy : Redundancy { :>> avionics = AvionicsRedundancy::crossStrapped; }
        }

        part MAV1 : AscentStage {
            // 1.5 m wide (was 1.4) so the larger load (4,300 kg, was 3,750) fits under the fairing; walls scaled with
            // the radius to keep the same hoop stress at 12.8 bar
            :>> outerDiameter = 1.5 [m];
            :>> propellantLoad = 4300 [kg];
            part fuelTank : PropellantTank {
                :>> propellant = "MMH (MIL-PRF-27404)";
                :>> material = "IM7/8552";
                :>> layup = "[45/-45/0/90]2s";
                :>> radius = 0.75 [m];
                :>> barrelLength = 0.376 [m];
                :>> wallThickness = 2.61 [mm];
            }
            part oxidizerTank : PropellantTank {
                :>> propellant = "NTO (MIL-PRF-26539)";
                :>> material = "IM7/8552";
                :>> layup = "[45/-45/0/90]2s";
                :>> radius = 0.75 [m];
                :>> barrelLength = 0.378 [m];
                :>> wallThickness = 2.61 [mm];
            }
            part pressurization : PressurizationSystem { :>> meop = 12.8 [bar]; }
            part mli : MultiLayerInsulation { :>> layers = 20; }
            part blanket : SurfaceThermalBlanket { :>> heatLeakCoefficient = 0.05 [W/(m^2*K)]; }
            part engine : AJ[2];
            connection : PropellantFeed connect fuelTank to engine;
            connection : PropellantFeed connect oxidizerTank to engine;
            part acs : AttitudeControlSystem { :>> maxSlewRate = 10.0 [deg/s]; }
            // Cross-strapped spare avionics, as on SRF: the flight software switches to it (switch_string).
            part redundancy : Redundancy { :>> avionics = AvionicsRedundancy::crossStrapped; }
        }

        part MAV2 : AscentStage {
            // 1.3 m wide (was 1.2) for 2,250 kg (was 1,850): about 60 m/s of trans-Earth margin with 25 kg of capsule
            // growth and cross-strapped avionics; walls scaled with the radius (same hoop stress)
            :>> outerDiameter = 1.3 [m];
            :>> propellantLoad = 2250 [kg];
            part fuelTank : PropellantTank {
                :>> propellant = "MMH (MIL-PRF-27404)";
                :>> material = "IM7/8552";
                :>> layup = "[45/-45/0/90]2s";
                :>> radius = 0.65 [m];
                :>> barrelLength = 0.142 [m];
                :>> wallThickness = 2.26 [mm];
            }
            part oxidizerTank : PropellantTank {
                :>> propellant = "NTO (MIL-PRF-26539)";
                :>> material = "IM7/8552";
                :>> layup = "[45/-45/0/90]2s";
                :>> radius = 0.65 [m];
                :>> barrelLength = 0.144 [m];
                :>> wallThickness = 2.26 [mm];
            }
            part pressurization : PressurizationSystem { :>> meop = 12.8 [bar]; }
            part mli : MultiLayerInsulation { :>> layers = 20; }
            part blanket : SurfaceThermalBlanket { :>> heatLeakCoefficient = 0.05 [W/(m^2*K)]; }
            part engine : AJ[1];
            connection : PropellantFeed connect fuelTank to engine;
            connection : PropellantFeed connect oxidizerTank to engine;
            part rcs : ReactionControlSystem {
                :>> propellantLoad = 40.0 [kg];
                :>> totalThrust = 200.0 [N];
            }
            part array : SolarArray { :>> area = 3.5 [m^2]; }
            part battery : Battery { :>> energy = 1.0 [kW*h]; }
            part acs : AttitudeControlSystem { :>> maxSlewRate = 10.0 [deg/s]; }
            // Redundancy: a second RCS branch for the 7-month return cruise, and a cold spare avionics string
            // (a single string lost anywhere from the pad to the Earth approach ends the mission, about 1.5%).
            part redundancy : Redundancy {
                :>> avionics = AvionicsRedundancy::crossStrapped;
                :>> rcsBranches = 2;
            }
        }

        part CAP : EarthEntryCapsule {
            doc /* Earth entry capsule: PICA forebody, parachute landing on CMAIN. Crushable, the bare capsule, which
                 * tumbles below Mach 0.9, reached the lakebed at 45 to 48 m/s against the 45 m/s limit in half the
                 * worlds. A 2.8 m crushable capsule would land slower but weighs about 59 kg more (TPS, aeroshell
                 * and avionics all grow with the area); the parachute costs 7.4 kg net. */
            :>> baseDiameter = 2.4 [m];
            :>> halfAngle = 70.0 [deg];
            :>> noseRadius = 1.2 [m];
            :>> landing = EarthLanding::parachute;
            part tps : ThermalProtection {
                :>> material = "PICA";
                :>> thickness = 38.89 [mm];
            }
            // Sample carrier finish (models/samples/thermal.py): solar absorptance over emittance 0.3, a white finish.
            part containment : SampleContainmentAssembly {
                :>> mass = 25.0 [kg];
                :>> sampleCarrierAlphaOverEpsilon = 0.3;
            }
            requirement loads : DecelerationLimit { :>> maxLoadFactor = 150.0; }
            // Redundancy: cross-strapped avionics. With a cold spare only (dual), a unit lost on each string over
            // the 7-month return lost the capsule (world 1010 of the 48-world baseline); cross-strapped, the capsule
            // is lost only when the same unit fails on both sides (+6 kg).
            part redundancy : Redundancy { :>> avionics = AvionicsRedundancy::crossStrapped; }
        }

        part CMAIN : Parachute {
            doc /* The capsule's main parachute (it rides on CAP, the capsule below it): a 12 m ringsail, opened 1.5 km
                 * above the Utah ground (gnc/earth_entry.py) for a touchdown near 8 m/s, inside the 10 m/s of a
                 * parachute landing. 18.7 kg against the 11.3 kg crushable absorber it replaces. */
            :>> canopy = CanopyType::ringsail;
            :>> nominalDiameter = 12.0 [m];
            :>> designDynamicPressure = 2000.0 [Pa];
        }

        part BS : Backshell {
            doc /* Backshell TPS sized for the backshell heating (models/plumes/backshell.py): 2.0 kg/m^2 of
                 * SLA-561V, near MSL's 2.11 kg/m^2; the flown worlds need 1.45 to 1.69 kg/m^2. */
            part tps : ThermalProtection {
                :>> material = "SLA-561V";
                :>> thickness = 7.72 [mm];
            }
            part rcs : ReactionControlSystem {
                :>> propellantLoad = 450.0 [kg];
                :>> totalThrust = 8800.0 [N];
            }
            // Redundancy: a second RCS branch for the 9-month cruise to Mars, as vehicle.json's
            // "redundancy": {"rcs_branches": 2}.
            part redundancy : Redundancy { :>> rcsBranches = 2; }
        }

        part CHUTE : Parachute {
            :>> canopy = CanopyType::diskGapBand;
            :>> nominalDiameter = 60.0 [m];
            :>> designDynamicPressure = 850.0 [Pa];
        }

        // ------------------------------------------------------------ the stack, from the pad up
        connection : StageJoint connect S1.fwd to S2.aft;
        connection : StageJoint connect S2.fwd to FAIR.aft;
        connection : StageJoint connect FAIR.fwd to HS.aft;
        connection : StageJoint connect HS.fwd to DL.aft;
        connection : StageJoint connect DL.fwd to SRF.aft;
        connection : StageJoint connect SRF.fwd to MAV1.aft;
        connection : StageJoint connect MAV1.fwd to MAV2.aft { :>> interstage = true; }
        connection : StageJoint connect MAV2.fwd to CAP.aft;
        connection : StageJoint connect CAP.fwd to CMAIN.aft;
        connection : StageJoint connect CMAIN.fwd to BS.aft;
        connection : StageJoint connect BS.fwd to CHUTE.aft;
    }
}
