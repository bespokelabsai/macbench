package AscentExample {
    doc /*
     * The manual's ascent example (task/environment/app/examples/ascent_vehicle.json) in the design
     * language: two engines the team develops, a methane booster and a hydrogen upper stage.
     */
    private import ScalarValues::*;
    private import SI::*;
    private import MarsReturnUnits::*;
    private import MarsReturnLibrary::*;

    part def B1 :> LiquidRocketEngine {
        doc /* Booster engine: full-flow staged combustion, methane and oxygen. */
        :>> fuel = "LCH4";
        :>> oxidizer = "LOX (MIL-PRF-25508)";
        :>> cycle = EngineCycle::fullFlowStagedCombustion;
        :>> chamberPressure = 250 [bar];
        :>> mixtureRatio = 3.6;
        :>> areaRatio = 30;
        :>> throatDiameter = 0.22 [m];
        :>> cooling = ChamberCooling::regenerative;
        :>> ignition = IgnitionSystem::sparkTorch;
        :>> gimbalRange = 8 [deg];
        :>> chamberMaterial = "GRCop-42";
        :>> nozzleMaterial = "Inconel 718";
    }

    part def U1 :> LiquidRocketEngine {
        doc /* Upper stage engine: expander cycle, hydrogen and oxygen. */
        :>> fuel = "LH2 (MIL-PRF-27201)";
        :>> oxidizer = "LOX (MIL-PRF-25508)";
        :>> cycle = EngineCycle::expander;
        :>> chamberPressure = 60 [bar];
        :>> mixtureRatio = 5.8;
        :>> areaRatio = 150;
        :>> throatDiameter = 0.16 [m];
        :>> cooling = ChamberCooling::regenerative;
        :>> ignition = IgnitionSystem::sparkTorch;
        :>> gimbalRange = 5 [deg];
        :>> chamberMaterial = "NARloy-Z";
        :>> nozzleMaterial = "C-103";
    }

    part vehicle : MissionStack {
        :>> launchEpoch = "2031-02-10T00:00:00";

        part S1 : LaunchStage {
            :>> outerDiameter = 6.0 [m];
            :>> propellantLoad = 900 [t];
            part fuelTank : PropellantTank {
                :>> propellant = "LCH4";
                :>> material = "AISI 301 full hard";
                :>> radius = 3.0 [m];
                :>> barrelLength = 14.038 [m];
                :>> wallThickness = 5.5 [mm];
            }
            part oxidizerTank : PropellantTank {
                :>> propellant = "LOX";
                :>> material = "AISI 301 full hard";
                :>> radius = 3.0 [m];
                :>> barrelLength = 19.66 [m];
                :>> wallThickness = 5.5 [mm];
            }
            part pressurization : PressurizationSystem { :>> meop = 4.0 [bar]; }
            part engines : B1[12];
            connection : PropellantFeed connect fuelTank to engines;
            connection : PropellantFeed connect oxidizerTank to engines;
        }

        part S2 : LaunchStage {
            :>> outerDiameter = 5.0 [m];
            :>> propellantLoad = 120 [t];
            part fuelTank : PropellantTank {
                :>> propellant = "LH2";
                :>> material = "2195-T84";
                :>> radius = 2.5 [m];
                :>> barrelLength = 10.709 [m];
                :>> wallThickness = 2.8 [mm];
            }
            part oxidizerTank : PropellantTank {
                :>> propellant = "LOX";
                :>> material = "2195-T84";
                :>> radius = 2.5 [m];
                :>> barrelLength = 2.349 [m];
                :>> wallThickness = 2.8 [mm];
            }
            part pressurization : PressurizationSystem { :>> meop = 3.0 [bar]; }
            part foam : SprayOnFoamInsulation;
            part engines : U1[4];
            connection : PropellantFeed connect fuelTank to engines;
            connection : PropellantFeed connect oxidizerTank to engines;
            part rcs : ReactionControlSystem {
                :>> propellantLoad = 300 [kg];
                :>> totalThrust = 2 [kN];
            }
        }

        part FAIR : PayloadFairing {
            :>> outerDiameter = 5.0 [m];
            :>> length = 6.0 [m];
        }

        part CAP : EarthEntryCapsule {
            :>> baseDiameter = 1.5 [m];
            :>> halfAngle = 60 [deg];
            :>> noseRadius = 0.4 [m];
            :>> landing = EarthLanding::parachute;
            part tps : ThermalProtection {
                :>> material = "PICA";
                :>> thickness = 55.56 [mm];
            }
            part containment : SampleContainmentAssembly { :>> mass = 60 [kg]; }
            requirement loads : DecelerationLimit { :>> maxLoadFactor = 60; }
        }

        connection : StageJoint connect S1.fwd to S2.aft { :>> interstage = true; }
        connection : StageJoint connect S2.fwd to FAIR.aft;
        connection : StageJoint connect FAIR.fwd to CAP.aft;
    }
}
