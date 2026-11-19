library package MarsReturnLibrary {
    doc /*
     * Domain library for describing a Mars sample return vehicle in SysML v2.
     * A team describes its vehicle with these definitions: what each part is made of, its shape,
     * what it carries, and how the parts join. Masses, strength, aerodynamics and engine
     * performance are not stated anywhere in a model. The program office works them out from
     * the description, and tests of built hardware measure them.
     *
     * Designations are strings, checked against public lists:
     *   metals by their MMPDS designation ("2219-T87", "2195-T84", "AISI 301 full hard", "Ti-6Al-4V annealed"),
     *   composites by their CMH-17 material system ("IM7/8552") with a layup,
     *   thermal protection by material name ("PICA", "SLA-561V", "AVCOAT 5026-39", "carbon phenolic"),
     *   propellants by name and procurement specification ("LOX (MIL-PRF-25508)").
     */
    private import ScalarValues::*;
    private import ISQ::*;
    private import SI::*;
    private import MarsReturnUnits::*;

    // ------------------------------------------------------------------ enumerations

    enum def EngineCycle {
        enum pressureFed;
        enum electricPump;
        enum gasGenerator;
        enum expander;
        enum fuelRichStagedCombustion;
        enum oxidizerRichStagedCombustion;
        enum fullFlowStagedCombustion;
    }
    enum def ChamberCooling {
        enum regenerative;
        enum ablative;
        enum radiative;
    }
    enum def InjectorType {
        enum conventional;
        enum pintle;
    }
    enum def IgnitionSystem {
        enum hypergolic;
        enum sparkTorch;
        enum teaTeb;
    }
    enum def ChilldownMode {
        enum dump;
        enum recirculation;
    }
    enum def CanopyType {
        enum diskGapBand;
        enum ringsail;
    }
    enum def EarthLanding {
        enum parachute;
        enum crushable;
    }
    enum def PropellantGauge {
        enum capacitance;
        enum pointSensors;
        enum pvt;
    }
    enum def AvionicsRedundancy {
        enum single;
        enum dual;
        enum crossStrapped;
    }
    enum def TelecomBand {
        enum X;
        enum Ka;
    }

    // ------------------------------------------------------------------ interfaces and joints

    port def StackInterface {
        doc /* A joint plane at one end of a stack element: its bolt ring, separation hardware and umbilicals. */
    }

    connection def StageJoint {
        doc /*
         * A separation plane between two stack elements. Joints list the launch stack from the bottom
         * up: the lower element's fwd port joins the upper element's aft port. A fairing covers every
         * element above it; a heatshield and the next backshell above it enclose what lies between them.
         * interstage = true puts an interstage structure (a skirt) under the upper element. It stays
         * with the lower element when they separate.
         */
        end port lower : StackInterface;
        end port upper : StackInterface;
        attribute interstage : Boolean default = false;
    }

    connection def PropellantFeed {
        doc /* A feed line from a propellant tank to the engines it supplies, on the same stage. */
        end part source : PropellantTank;
        end part sink : RocketEngine;
    }

    // ------------------------------------------------------------------ tanks and structure

    part def PropellantTank {
        doc /*
         * A cylindrical barrel closed by two ellipsoidal domes. radius is the inner radius; a tank with
         * barrelLength = 0 is two domes only and may be narrower than its stage. domeAspectRatio is the
         * dome height over the radius. wallThickness is the skin gauge (the membrane thickness).
         */
        attribute propellant : String;
        attribute material : String;
        attribute layup : String [0..1];
        attribute radius : LengthValue;
        attribute barrelLength : LengthValue;
        attribute domeAspectRatio : Real default = 0.7071;
        attribute wallThickness : LengthValue;
        attribute ullageFraction : Real default = 0.03;
    }

    part def StringerRingStiffening {
        doc /* Stringers along the axis and rings around it, inside the tank barrel (S-IC, Shuttle ET). */
        attribute stringerHeight : LengthValue;
        attribute stringerThickness : LengthValue;
        attribute stringerSpacing : LengthValue;
        attribute ringSpacing : LengthValue;
        attribute stringerFlangeWidth : LengthValue [0..1];
        attribute ringHeight : LengthValue [0..1];
        attribute ringThickness : LengthValue [0..1];
    }

    part def RingBaffles {
        doc /* Flat ring slosh baffles: width as a share of the tank radius, spaced along the tank. */
        attribute widthFraction : Real;
        attribute spacing : LengthValue;
    }

    part def CommonBulkhead {
        doc /* One dome separates the two propellants inside a single tank body (S-II, Centaur, Falcon 9). */
    }

    part def PressurizationSystem {
        doc /* Tank pressurization. meop is the maximum expected operating pressure of the propellant tanks. */
        attribute meop : PressureValue;
        attribute pressurant : String default = "GHe";
        attribute balloonTanks : Boolean default = false;
        // pyroIsolationValves: normally closed pyrotechnic valves between the pressurant and the propellant
        // tanks, fired open at the stage's first start after a long cruise (the fix after Mars Observer).
        attribute pyroIsolationValves : Boolean default = false;
    }

    // ------------------------------------------------------------------ engines

    abstract part def RocketEngine {
        doc /* A liquid rocket engine, bought or developed. */
        attribute chilldown : ChilldownMode [0..1];
        // pulsed: the engine fires in short pulses near the ground (Phoenix-style pulse-modulated descent),
        // which digs the surface more than steady jets do.
        attribute pulsed : Boolean default = false;
    }

    part def ProcuredEngine :> RocketEngine {
        doc /* A flight-proven engine bought by its designation, e.g. "Raptor 2" or "AJ10-190". */
        attribute designation : String;
    }

    part def LiquidRocketEngine :> RocketEngine {
        doc /*
         * An engine the team develops, described by what a NASA CEA or RPA run takes (propellants,
         * chamber pressure, mixture ratio, area ratio) plus the hardware choices that set its size,
         * cooling, ignition, throttling and steering.
         */
        attribute fuel : String;
        attribute oxidizer : String;
        attribute cycle : EngineCycle;
        attribute chamberPressure : PressureValue;
        attribute mixtureRatio : Real;
        attribute areaRatio : Real;
        attribute throatDiameter : LengthValue;
        attribute cooling : ChamberCooling;
        attribute filmCoolingFraction : Real default = 0.0;
        attribute injector : InjectorType default = InjectorType::conventional;
        attribute ignition : IgnitionSystem;
        attribute igniterUnits : Integer default = 1;
        attribute gimbalRange : AngularMeasureValue default = 0.0 [deg];
        attribute chamberMaterial : String;
        attribute nozzleMaterial : String;
    }

    // ------------------------------------------------------------------ subsystems

    part def AttitudeControlSystem {
        doc /* Reaction wheels, gimbals and RCS together: the fastest turn the vehicle commands. */
        attribute maxSlewRate : AngularVelocityValue;
    }
    part def ReactionControlSystem {
        doc /* Monopropellant thrusters for small burns and ullage settling. */
        attribute propellant : String default = "N2H4 (MIL-PRF-26536)";
        attribute propellantLoad : MassValue;
        attribute totalThrust : ForceValue;
    }
    part def MultiLayerInsulation {
        doc /* MLI blankets on every propellant tank of the element. */
        attribute layers : Integer;
    }
    part def SprayOnFoamInsulation {
        doc /* Spray-on foam on the cryogenic tanks of a launch stage. */
    }
    part def SurfaceThermalBlanket {
        doc /* Insulation against the Mars surface environment, as a heat leak per unit tank area. */
        attribute heatLeakCoefficient : CoefficientOfHeatTransferValue;
    }
    part def QualificationLevels {
        doc /* The vibration levels a payload element is qualified to: acousticLevel is the overall sound
               pressure level in dB (re 20 micropascal), shockLevel the peak of the shock response spectrum in
               Earth g. Left unset, acousticLevel is the element's predicted liftoff level plus 3 dB (at least
               137.4 dB) and shockLevel is 1413 g. Higher levels cost structure mass; lower ones save it. */
        attribute acousticLevel : Real [0..1];
        attribute shockLevel : Real [0..1];
    }
    part def PogoAccumulator {
        doc /* Gas-filled accumulators at the pump inlets of a stage's engines, one per engine and line: the gas
               volume of each, on the oxidizer line, the fuel line or both. They detune the feed lines from the
               stack's first longitudinal mode (pogo). */
        attribute oxidizerGasVolume : VolumeValue [0..1];
        attribute fuelGasVolume : VolumeValue [0..1];
    }
    part def LandingGear {
        doc /* Legs rated for a touchdown speed. */
        attribute touchdownSpeedRating : SpeedValue;
    }
    part def TerrainRelativeNavigation {
        doc /* A descent camera and vision computer that fix the lander's position against onboard terrain maps,
               like the Lander Vision System of Mars 2020. 15 kg, procured. */
    }
    part def MMRTG {
        doc /* A Multi-Mission Radioisotope Thermoelectric Generator, procured. */
    }
    part def FissionPowerUnit {
        doc /* A 10 kWe class fission power unit, procured. */
    }
    part def SolarArray {
        attribute area : AreaValue;
    }
    part def Battery {
        attribute energy : EnergyValue;
    }
    part def PropellantGauging {
        doc /* How the stage reads its tanks and stops its engines. gauge: capacitance probes (the default), point
               level sensors with flowmeters, or helium pressure-volume-temperature (works in zero g, less exact).
               lowLevelCutoff = true stops the engines when either tank runs low, before a pump runs dry. */
        attribute gauge : PropellantGauge default = PropellantGauge::capacitance;
        attribute lowLevelCutoff : Boolean default = true;
    }
    part def Redundancy {
        doc /* Spare hardware against part failures in flight. avionics: single, dual (a cold spare string) or
               crossStrapped (dual, with the units of both strings cross-connected). rcsBranches, landingRadars,
               pyroInitiators and isruUnits: 1 or 2; batteryStrings: 1 to 4. Each spare adds its mass. */
        attribute avionics : AvionicsRedundancy default = AvionicsRedundancy::single;
        attribute rcsBranches : Integer default = 1;
        attribute landingRadars : Integer default = 1;
        attribute pyroInitiators : Integer default = 1;
        attribute batteryStrings : Integer default = 1;
        attribute isruUnits : Integer default = 1;
    }
    part def TelecomSystem {
        doc /* The element's radio. Without one, an element keeps a 15 W X-band amplifier and low-gain antennas.
               highGainAntennaDiameter 0 means no high-gain antenna; transmitPower is the amplifier's RF output;
               gimbal points the high-gain antenna; uhfRelay adds a UHF radio for relay during entry. */
        attribute band : TelecomBand default = TelecomBand::X;
        attribute highGainAntennaDiameter : LengthValue default = 0.0 [m];
        attribute transmitPower : PowerValue default = 15.0 [W];
        attribute gimbal : Boolean default = true;
        attribute uhfRelay : Boolean default = false;
    }
    part def Cryocooler {
        attribute inputPower : PowerValue;
    }
    part def IsruOxygenPlant {
        doc /* A solid oxide electrolysis plant that makes oxygen from the Mars atmosphere. */
        attribute inputPower : PowerValue;
    }
    part def EquipmentItem {
        doc /* Equipment the library does not model, carried at its vendor-quoted mass. */
        attribute mass : MassValue;
    }
    part def ThermalProtection {
        doc /* An ablative or insulating heat shield layer of one material and a uniform thickness. */
        attribute material : String;
        attribute thickness : LengthValue;
    }
    part def SampleContainmentAssembly {
        doc /*
         * The sealed containment of the samples and its structure, a furnished item at its quoted mass.
         * sampleContainers is how many Orbiting Sample containers it brings home: each holds 30 tubes and
         * weighs about 11 kg loaded. Each container beyond the first adds 0.5 kg of containment per kg.
         * sampleCarrierAlphaOverEpsilon is the solar absorptance over the infrared emittance of the sample
         * carrier's finish, which sets the samples' temperature in sunlight (default 0.5).
         */
        attribute mass : MassValue;
        attribute sampleContainers : Integer default = 1;
        attribute sampleCarrierAlphaOverEpsilon : Real [0..1];
    }

    // ------------------------------------------------------------------ stack elements

    abstract part def StackElement {
        port aft : StackInterface;
        port fwd : StackInterface;
    }

    part def Stage :> StackElement {
        doc /*
         * Tanks, engines and whatever else is bolted on. propellantLoad is the propellant loaded at
         * launch, split between the tanks at the engines' mixture ratio; oxidizerLoadFraction < 1 leaves
         * the oxidizer tank part empty (to fill on Mars).
         */
        attribute outerDiameter : LengthValue;
        attribute propellantLoad : MassValue default = 0.0 [kg];
        attribute oxidizerLoadFraction : Real default = 1.0;
        attribute loadedMixtureRatio : Real [0..1];
        attribute differentialThrottle : Boolean default = false;
    }
    part def FinSet {
        doc /*
         * Fins at the bottom of a stage, equal and evenly spaced about the stack. span is one fin's
         * span from the body out; sweep is how far the tip's leading edge sits behind the root's. The
         * simulator gives them Barrowman's normal force when MARSFLIGHT_FINS is on.
         */
        attribute finCount : Integer default = 4;
        attribute span : LengthValue;
        attribute rootChord : LengthValue;
        attribute tipChord : LengthValue default = 0.0 [m];
        attribute sweep : LengthValue default = 0.0 [m];
    }
    part def LaunchStage :> Stage {
        doc /* recovery (first stage only, optional): "none" (the default), "rtls" (boostback, entry and landing
               burns back to the launch site) or "droneship" (entry and landing burns onto a ship downrange). A
               recovered stage holds back propellant for its return and carries legs and grid fins, and earns a
               reuse credit if it lands. Used only when the simulator's MARSFLIGHT_RECOVERY switch is on. */
        attribute recovery : String [0..1];
    }
    part def Lander :> Stage;
    part def AscentStage :> Stage;
    part def SurfacePlatform :> Stage {
        doc /* A surface element without propulsion: power, avionics and sample handling. */
    }

    part def PayloadFairing :> StackElement {
        attribute outerDiameter : LengthValue;
        attribute length : LengthValue;
    }

    part def Heatshield :> StackElement {
        doc /*
         * A sphere-cone forebody. trimAngleOfAttack is the angle the vehicle trims at, set by a
         * radial center-of-mass offset (entry ballast).
         */
        attribute baseDiameter : LengthValue;
        attribute halfAngle : AngularMeasureValue;
        attribute noseRadius : LengthValue;
        attribute trimAngleOfAttack : AngularMeasureValue default = 0.0 [deg];
    }

    part def Backshell :> StackElement {
        doc /* The aft cover of an aeroshell. Its size follows from what it encloses. */
    }

    part def Parachute :> StackElement {
        attribute canopy : CanopyType;
        attribute nominalDiameter : LengthValue;
        attribute designDynamicPressure : PressureValue;
    }

    part def EarthEntryCapsule :> StackElement {
        doc /* The Earth return capsule that carries the samples home. */
        attribute baseDiameter : LengthValue;
        attribute halfAngle : AngularMeasureValue;
        attribute noseRadius : LengthValue;
        attribute trimAngleOfAttack : AngularMeasureValue default = 0.0 [deg];
        attribute landing : EarthLanding;
    }

    requirement def DecelerationLimit {
        doc /* The capsule and its samples survive a peak load factor up to maxLoadFactor (in Earth g). */
        subject capsule : EarthEntryCapsule;
        attribute maxLoadFactor : Real;
    }

    part def MissionStack {
        doc /* The whole vehicle on the pad. launchEpoch is UTC, ISO 8601. dsnPassesPerDay books the Deep Space
               Network: 1 (Goldstone), 2 (Goldstone and Canberra) or 3 (every complex, the default).
               groundWindLimit is the highest ground wind the vehicle may launch in (default 17.5 m/s).
               launchSite is the pad: "LC-39A" (the default), "SLC-40", "LC-46" or "SLC-4E" (Vandenberg); each has
               its range's launch azimuth limits. */
        attribute launchEpoch : String;
        attribute dsnPassesPerDay : Integer [0..1];
        attribute groundWindLimit : SpeedValue [0..1];
        attribute launchSite : String [0..1];
    }
}
