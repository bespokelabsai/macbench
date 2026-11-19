package MarsSampleReturn {
    doc /*
     * The team's vehicle, in SysML v2 with the domain library in /opt/designlang/library.
     * docs/REFERENCE.md explains the language. examples/ascent_vehicle.sysml is a complete model to
     * start from. Check this file with: design-check
     */
    private import ScalarValues::*;
    private import SI::*;
    private import MarsReturnUnits::*;
    private import MarsReturnLibrary::*;

    part vehicle : MissionStack {
        :>> launchEpoch = "2033-04-20T00:00:00";
    }
}
