library package MarsReturnUnits {
    doc /*
     * Units that rocket engineers use and that the SysML v2 SI package does not define by these
     * symbols: bar, kilonewtons, kilo- and megapascals, tonnes and degrees of arc. Models import
     * them next to SI::*.
     */
    private import ISQ::*;
    private import SI::*;
    private import SIPrefixes::*;
    private import MeasurementReferences::*;

    attribute <bar> bar : PressureUnit { :>> unitConversion : ConversionByConvention { :>> referenceUnit = Pa; :>> conversionFactor = 1E5; } }
    attribute <kPa> kilopascal : PressureUnit { :>> unitConversion : ConversionByPrefix { :>> prefix = kilo; :>> referenceUnit = Pa; } }
    attribute <MPa> megapascal : PressureUnit { :>> unitConversion : ConversionByPrefix { :>> prefix = mega; :>> referenceUnit = Pa; } }
    attribute <kN> kilonewton : ForceUnit { :>> unitConversion : ConversionByPrefix { :>> prefix = kilo; :>> referenceUnit = N; } }
    attribute <MN> meganewton : ForceUnit { :>> unitConversion : ConversionByPrefix { :>> prefix = mega; :>> referenceUnit = N; } }
    attribute <t> tonne : MassUnit { :>> unitConversion : ConversionByConvention { :>> referenceUnit = kg; :>> conversionFactor = 1E3; } }
    attribute <deg> degreeOfArc : AngularMeasureUnit { :>> unitConversion : ConversionByConvention { :>> referenceUnit = rad; :>> conversionFactor = 1.745329252E-02; :>> isExact = false; } }
}
