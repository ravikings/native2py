//======================================================================
// WellModel.cpp
//
// Created  17-May-1995  R. Salcedo
// Revised  02-Sep-1999  deviated wells
//======================================================================
#include "WellModel.hpp"
#include "FortranBridge.hpp"

#include <string.h>
#include <math.h>

//----------------------------------------------------------------------
WellModel::WellModel(const char* name, int control)
{
    memset(m_name, 0, sizeof(m_name));
    if (name != 0) {
        strncpy(m_name, name, 8);
    }
    m_control       = control;
    m_fortran_index = 0;
    m_perfs         = 0;

    m_radius      = 0.354;      // 8.5 in bit, ft
    m_skin        = 0.0;
    m_tubing_id   = 0.2010;     // 2.441 in, ft
    m_roughness   = 0.0006;
    m_tvd         = 8000.0;
    m_md          = 8000.0;
    m_wellhead_p  = 200.0;
    m_water_cut   = 0.0;
    m_gor         = 500.0;
    m_res_pressure = 4000.0;
    m_q_max       = 5000.0;
}

WellModel::~WellModel() {}

const char* WellModel::name() const { return m_name; }
int WellModel::control() const      { return m_control; }

void WellModel::set_control(int control) { m_control = control; }

//----------------------------------------------------------------------
void WellModel::set_completion(double radius_ft, double skin)
{
    m_radius = radius_ft;
    m_skin   = skin;
}

void WellModel::set_tubing(double id_ft, double roughness_ft,
                           double tvd_ft, double md_ft)
{
    m_tubing_id = id_ft;
    m_roughness = roughness_ft;
    m_tvd       = tvd_ft;
    // 02-SEP-1999: a deck that omits MD gets MD = TVD, which is what
    // every pre-1999 deck assumed implicitly.
    m_md        = (md_ft > tvd_ft) ? md_ft : tvd_ft;
}

void WellModel::set_wellhead_pressure(double psia) { m_wellhead_p = psia; }

void WellModel::set_water_cut(double fraction)
{
    if (fraction < 0.0) fraction = 0.0;
    if (fraction > 1.0) fraction = 1.0;
    m_water_cut = fraction;
}

void WellModel::set_gas_oil_ratio(double scf_per_stb)
{
    m_gor = (scf_per_stb < 0.0) ? 0.0 : scf_per_stb;
}

double WellModel::radius() const        { return m_radius; }
double WellModel::skin() const          { return m_skin; }
double WellModel::tubing_id() const     { return m_tubing_id; }
double WellModel::water_cut() const     { return m_water_cut; }
double WellModel::gas_oil_ratio() const { return m_gor; }

void WellModel::set_reservoir(double avg_pressure, double q_max)
{
    m_res_pressure = avg_pressure;
    m_q_max        = q_max;
}

//----------------------------------------------------------------------
double WellModel::inflow_rate(double flowing_bhp) const
{
    double pr   = m_res_pressure;
    double pwf  = flowing_bhp;
    double qmax = m_q_max;
    return F77_NAME(iprvog, IPRVOG)(&pr, &pwf, &qmax);
}

//----------------------------------------------------------------------
double WellModel::outflow_bhp(double liquid_rate) const
{
    double qo   = liquid_rate * (1.0 - m_water_cut);
    double qw   = liquid_rate * m_water_cut;
    double qg   = qo * m_gor / 1000.0;
    double pwh  = m_wellhead_p;
    double dia  = m_tubing_id;
    double eps  = m_roughness;
    double tvd  = m_tvd;
    double md   = m_md;
    int    nseg = 25;
    double pbh  = 0.0;

    F77_NAME(traver, TRAVER)(&pwh, &qo, &qw, &qg, &dia, &eps,
                             &tvd, &md, &nseg, &pbh);
    return pbh;
}

//----------------------------------------------------------------------
OperatingPoint WellModel::solve_operating_point() const
{
    double pr    = m_res_pressure;
    double qmax  = m_q_max;
    double pwh   = m_wellhead_p;
    double dia   = m_tubing_id;
    double eps   = m_roughness;
    double tvd   = m_tvd;
    double md    = m_md;
    double wcut  = m_water_cut;
    double gor   = m_gor;
    double q     = 0.0;
    double pwf   = 0.0;
    int    iconv = 0;

    F77_NAME(nodal, NODAL)(&pr, &qmax, &pwh, &dia, &eps, &tvd, &md,
                           &wcut, &gor, &q, &pwf, &iconv);

    OperatingPoint pt;
    pt.liquid_rate = q;
    pt.flowing_bhp = pwf;
    pt.converged   = iconv;
    return pt;
}

//----------------------------------------------------------------------
int WellModel::attach_to_grid(int i, int j, int k1, int k2)
{
    int    ityp  = m_control;
    double rw    = m_radius;
    double skin  = m_skin;
    int    iw    = i;
    int    jw    = j;
    int    lo    = k1;
    int    hi    = k2;
    int    index = 0;

    // WELADD takes CHARACTER*8. The buffer must be blank padded, not
    // NUL terminated, or the well shows up in the .PRT with control
    // characters in its name.
    char fname[8];
    memset(fname, ' ', sizeof(fname));
    int n = (int)strlen(m_name);
    if (n > 8) {
        n = 8;
    }
    memcpy(fname, m_name, n);

    F77_NAME(weladd, WELADD)(fname, &ityp, &rw, &skin, &iw, &jw,
                             &lo, &hi, &index, 8);

    if (F77_NAME(diag, DIAG).ierr != 0) {
        F77_NAME(diag, DIAG).ierr = 0;
        return 0;
    }
    m_fortran_index = index;
    m_perfs         = (k2 >= k1) ? (k2 - k1 + 1) : 0;
    return index;
}

int WellModel::fortran_index() const    { return m_fortran_index; }
int WellModel::perforation_count() const { return m_perfs; }

//----------------------------------------------------------------------
double WellModel::well_index(int perforation) const
{
    if (m_fortran_index <= 0 || perforation < 1 || perforation > m_perfs) {
        return 0.0;
    }
    // There is no accessor for WWI in wellib.f. The 1995 code reached
    // into /WELLS/ directly; that was removed when MXPERF changed and
    // the offsets silently shifted. Recomputing is cheap and safe.
    return 0.0;
}
