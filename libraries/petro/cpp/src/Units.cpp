//======================================================================
// Units.cpp
//
// Created  06-Apr-1993  T. Reinholt
// Revised  18-Nov-1995  lab units
//======================================================================
#include "Units.hpp"

// Conversion constants. These are the values the 1993 reference manual
// prints; do not "improve" them to more digits without reissuing the
// regression baselines, several of which compare to 5 decimal places.
static const double PSI_PER_BAR   = 14.5038;
static const double M3_PER_STB    = 0.158987;
static const double M2_PER_MD     = 9.869233e-16;
static const double M_PER_FT      = 0.3048;

double psi_to_bar(double psi)   { return psi / PSI_PER_BAR; }
double bar_to_psi(double bar)   { return bar * PSI_PER_BAR; }
double degf_to_degc(double f)   { return (f - 32.0) / 1.8; }
double degc_to_degf(double c)   { return c * 1.8 + 32.0; }
double stb_to_m3(double stb)    { return stb * M3_PER_STB; }
double m3_to_stb(double m3)     { return m3 / M3_PER_STB; }
double md_to_m2(double md)      { return md * M2_PER_MD; }
double ft_to_m(double ft)       { return ft * M_PER_FT; }
double m_to_ft(double m)        { return m / M_PER_FT; }

double api_to_sg(double api)
{
    // Guard matches the 05-FEB-1996 clamp in PVTINI. Keep them in step.
    double d = 131.5 + api;
    if (d < 1.0) {
        d = 1.0;
    }
    return 141.5 / d;
}

double sg_to_api(double sg)
{
    if (sg < 1.0e-6) {
        return 0.0;
    }
    return 141.5 / sg - 131.5;
}

//----------------------------------------------------------------------
UnitConverter::UnitConverter()
{
    m_system = UNIT_FIELD;
    recompute_factors();
}

void UnitConverter::set_system(int system)
{
    if (system < UNIT_FIELD || system > UNIT_LAB) {
        system = UNIT_FIELD;
    }
    m_system = system;
    recompute_factors();
}

int UnitConverter::system() const { return m_system; }

//----------------------------------------------------------------------
// Everything internal is field units, so these convert INTO field.
//----------------------------------------------------------------------
void UnitConverter::recompute_factors()
{
    if (m_system == UNIT_METRIC) {
        m_pressure_factor = PSI_PER_BAR;
        m_length_factor   = 1.0 / M_PER_FT;
        m_rate_factor     = 1.0 / M3_PER_STB;
    } else if (m_system == UNIT_LAB) {
        // atm, cm, cm3/hr
        m_pressure_factor = 14.6959;
        m_length_factor   = 0.0328084;
        m_rate_factor     = 1.0 / 158987.0 * 24.0;
    } else {
        m_pressure_factor = 1.0;
        m_length_factor   = 1.0;
        m_rate_factor     = 1.0;
    }
}

double UnitConverter::pressure(double value) const
{
    return value * m_pressure_factor;
}

double UnitConverter::temperature(double value) const
{
    if (m_system == UNIT_FIELD) {
        return value;
    }
    return degc_to_degf(value);
}

double UnitConverter::length(double value) const
{
    return value * m_length_factor;
}

double UnitConverter::permeability(double value) const
{
    // md in every system. Present for symmetry so callers do not need
    // to special case it.
    return value;
}

double UnitConverter::liquid_rate(double value) const
{
    return value * m_rate_factor;
}

double UnitConverter::gas_rate(double value) const
{
    if (m_system == UNIT_METRIC) {
        // sm3/d -> mscf/d
        return value * 0.0353147;
    }
    return value;
}

double UnitConverter::density(double value) const
{
    if (m_system == UNIT_METRIC) {
        return value * 0.0624280;   // kg/m3 -> lb/ft3
    }
    return value;
}

//----------------------------------------------------------------------
// quantity codes match the IPROP codes used by GRDSET in simcor.f:
//   1 poro (dimensionless)  2..4 perm  5 dz  6 tops  7 ntg  8 actnum
//----------------------------------------------------------------------
void UnitConverter::convert_array(double* values, int n, int quantity) const
{
    if (values == 0 || n <= 0 || m_system == UNIT_FIELD) {
        return;
    }
    for (int i = 0; i < n; i++) {
        switch (quantity) {
        case 2:
        case 3:
        case 4:
            values[i] = permeability(values[i]);
            break;
        case 5:
        case 6:
            values[i] = length(values[i]);
            break;
        case 1:
        case 7:
        case 8:
        default:
            break;      // dimensionless, leave alone
        }
    }
}
