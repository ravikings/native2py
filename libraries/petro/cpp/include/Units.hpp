//======================================================================
// Units.hpp - field / metric / lab unit conversion
//
// Created  06-Apr-1993  T. Reinholt
// Revised  18-Nov-1995  added lab units for core analysis
// Revised  02-Feb-1999  Y2K sweep - no date handling in this header
//
// Written for cfront-era C++. No namespaces, no templates, no STL,
// no exceptions - the 1993 target compilers on HP-UX and VMS
// supported none of them, and the AIX build still uses xlC in
// pre-standard mode. Please keep it that way; Simulator.cpp and
// DeckReader.cpp both include this and both still build with -qlanglvl.
//======================================================================
#ifndef PETRO_UNITS_HPP
#define PETRO_UNITS_HPP

enum UnitSystem { UNIT_FIELD = 0, UNIT_METRIC = 1, UNIT_LAB = 2 };

//---------------------------------------------------------------------
// Stateless conversions. All take and return doubles so they can be
// used from the Fortran interop layer without a wrapper.
//---------------------------------------------------------------------
double psi_to_bar(double psi);
double bar_to_psi(double bar);
double degf_to_degc(double f);
double degc_to_degf(double c);
double stb_to_m3(double stb);
double m3_to_stb(double m3);
double md_to_m2(double md);
double ft_to_m(double ft);
double m_to_ft(double m);
double api_to_sg(double api);
double sg_to_api(double sg);

//---------------------------------------------------------------------
// UnitConverter - stateful form used by the deck reader, which has to
// convert whole arrays according to whatever the FIELD/METRIC card
// said. Kept as a class because the 1993 deck reader wanted one
// object per input file.
//---------------------------------------------------------------------
class UnitConverter {
public:
    UnitConverter();

    void set_system(int system);
    int system() const;

    double pressure(double value) const;
    double temperature(double value) const;
    double length(double value) const;
    double permeability(double value) const;
    double liquid_rate(double value) const;
    double gas_rate(double value) const;
    double density(double value) const;

    // In place array conversion. Used on the GRID arrays before they
    // are handed to GRDSET. n is the element count, not a byte size.
    void convert_array(double* values, int n, int quantity) const;

private:
    int m_system;
    double m_pressure_factor;
    double m_length_factor;
    double m_rate_factor;

    void recompute_factors();
};

#endif // PETRO_UNITS_HPP
