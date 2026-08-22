//======================================================================
// FluidModel.cpp
//
// Created  09-Feb-1994  T. Reinholt
// Revised  17-Jun-1997  pressure table cache
// Revised  22-Oct-2001  const correctness
//======================================================================
#include "FluidModel.hpp"
#include "FortranBridge.hpp"

#include <math.h>
#include <string.h>
#include <stdio.h>

const FluidModel* FluidModel::s_active = 0;

static const double PATM = 14.6959;

//----------------------------------------------------------------------
FluidModel::FluidModel(double api_gravity, double gas_gravity,
                       double temp_f)
{
    m_api         = api_gravity;
    m_gas_sg      = gas_gravity;
    m_temp_f      = temp_f;
    m_correlation = CORR_STANDING;
    m_bubble_point = 0.0;
    m_table_n     = 0;
    m_table_p     = 0;
    m_table_bo    = 0;
    m_table_rs    = 0;
    m_table_mu    = 0;
    m_table_z     = 0;
    m_err_text[0] = '\0';
    activate();
}

//----------------------------------------------------------------------
FluidModel::FluidModel(double api_gravity, double gas_gravity,
                       double temp_f, int correlation)
{
    m_api         = api_gravity;
    m_gas_sg      = gas_gravity;
    m_temp_f      = temp_f;
    m_correlation = correlation;
    m_bubble_point = 0.0;
    m_table_n     = 0;
    m_table_p     = 0;
    m_table_bo    = 0;
    m_table_rs    = 0;
    m_table_mu    = 0;
    m_table_z     = 0;
    m_err_text[0] = '\0';
    activate();
}

//----------------------------------------------------------------------
FluidModel::~FluidModel()
{
    clear_table();
    if (s_active == this) {
        s_active = 0;
    }
}

//----------------------------------------------------------------------
// Reload /FLUID/ from this object. PVTINI also recomputes the bubble
// point, which we cache because PVTBUB runs a Newton loop.
//----------------------------------------------------------------------
void FluidModel::activate() const
{
    if (s_active == this) {
        return;
    }
    double api  = m_api;
    double sgg  = m_gas_sg;
    double t    = m_temp_f;
    int    icor = m_correlation;

    F77_NAME(pvtini, PVTINI)(&api, &sgg, &t, &icor);

    m_bubble_point = F77_NAME(fluid, FLUID).pb;
    s_active = this;
}

//----------------------------------------------------------------------
double FluidModel::api_gravity() const  { return m_api; }
double FluidModel::gas_gravity() const  { return m_gas_sg; }
double FluidModel::temperature() const  { return m_temp_f; }

//----------------------------------------------------------------------
double FluidModel::bubble_point() const
{
    activate();
    return m_bubble_point;
}

//----------------------------------------------------------------------
double FluidModel::solution_gor(double pressure) const
{
    activate();
    if (m_table_n > 0) {
        return interpolate(m_table_rs, pressure);
    }
    double p = pressure;
    return F77_NAME(pvtrs, PVTRS)(&p);
}

//----------------------------------------------------------------------
double FluidModel::oil_fvf(double pressure) const
{
    activate();
    if (m_table_n > 0) {
        return interpolate(m_table_bo, pressure);
    }
    double p = pressure;
    return F77_NAME(pvtbo, PVTBO)(&p);
}

//----------------------------------------------------------------------
double FluidModel::oil_viscosity(double pressure) const
{
    activate();
    if (m_table_n > 0) {
        return interpolate(m_table_mu, pressure);
    }
    double p = pressure;
    return F77_NAME(pvtvis, PVTVIS)(&p);
}

//----------------------------------------------------------------------
double FluidModel::z_factor(double pressure) const
{
    activate();
    if (m_table_n > 0) {
        return interpolate(m_table_z, pressure);
    }
    double p = pressure;
    return F77_NAME(pvtz, PVTZ)(&p);
}

//----------------------------------------------------------------------
// The remaining single property accessors have no dedicated Fortran
// entry point, so they go through PVTSET and read /PVTOUT/.
//----------------------------------------------------------------------
double FluidModel::gas_viscosity(double pressure) const
{
    activate();
    double p = pressure;
    F77_NAME(pvtset, PVTSET)(&p);
    return F77_NAME(pvtout, PVTOUT).visg;
}

double FluidModel::gas_fvf(double pressure) const
{
    activate();
    double p = pressure;
    F77_NAME(pvtset, PVTSET)(&p);
    return F77_NAME(pvtout, PVTOUT).bg;
}

double FluidModel::oil_density(double pressure) const
{
    activate();
    double p = pressure;
    F77_NAME(pvtset, PVTSET)(&p);
    return F77_NAME(pvtout, PVTOUT).rhoo;
}

//----------------------------------------------------------------------
PvtState FluidModel::properties_at(double pressure) const
{
    activate();
    double p = pressure;
    F77_NAME(pvtset, PVTSET)(&p);

    PvtState s;
    s.bo        = F77_NAME(pvtout, PVTOUT).bo;
    s.bg        = F77_NAME(pvtout, PVTOUT).bg;
    s.bw        = F77_NAME(pvtout, PVTOUT).bw;
    s.rs        = F77_NAME(pvtout, PVTOUT).rsol;
    s.mu_oil    = F77_NAME(pvtout, PVTOUT).viso;
    s.mu_gas    = F77_NAME(pvtout, PVTOUT).visg;
    s.mu_water  = F77_NAME(pvtout, PVTOUT).visw;
    s.rho_oil   = F77_NAME(pvtout, PVTOUT).rhoo;
    s.rho_gas   = F77_NAME(pvtout, PVTOUT).rhog;
    s.rho_water = F77_NAME(pvtout, PVTOUT).rhow;
    s.z_factor  = F77_NAME(pvtout, PVTOUT).zfac;
    s.c_oil     = F77_NAME(pvtout, PVTOUT).co;
    return s;
}

//----------------------------------------------------------------------
int FluidModel::build_table(double p_min, double p_max, int n)
{
    clear_table();
    if (n < 2 || p_max <= p_min) {
        return -1;
    }
    activate();

    m_table_p  = new double[n];
    m_table_bo = new double[n];
    m_table_rs = new double[n];
    m_table_mu = new double[n];
    m_table_z  = new double[n];

    double dp = (p_max - p_min) / (double)(n - 1);
    for (int i = 0; i < n; i++) {
        double p = p_min + dp * (double)i;
        if (p < PATM) {
            p = PATM;
        }
        F77_NAME(pvtset, PVTSET)(&p);
        if (F77_NAME(diag, DIAG).ierr != 0) {
            clear_table();
            return F77_NAME(diag, DIAG).ierr;
        }
        m_table_p[i]  = p;
        m_table_bo[i] = F77_NAME(pvtout, PVTOUT).bo;
        m_table_rs[i] = F77_NAME(pvtout, PVTOUT).rsol;
        m_table_mu[i] = F77_NAME(pvtout, PVTOUT).viso;
        m_table_z[i]  = F77_NAME(pvtout, PVTOUT).zfac;
    }
    m_table_n = n;
    return 0;
}

//----------------------------------------------------------------------
int FluidModel::table_size() const { return m_table_n; }

//----------------------------------------------------------------------
void FluidModel::clear_table()
{
    delete[] m_table_p;
    delete[] m_table_bo;
    delete[] m_table_rs;
    delete[] m_table_mu;
    delete[] m_table_z;
    m_table_p  = 0;
    m_table_bo = 0;
    m_table_rs = 0;
    m_table_mu = 0;
    m_table_z  = 0;
    m_table_n  = 0;
}

//----------------------------------------------------------------------
// Linear interpolation on the cached table. Flat outside the range.
// Binary search, same convention as TLOOK in relperm.f.
//----------------------------------------------------------------------
double FluidModel::interpolate(const double* column, double pressure) const
{
    if (m_table_n <= 0) {
        return 0.0;
    }
    if (pressure <= m_table_p[0]) {
        return column[0];
    }
    if (pressure >= m_table_p[m_table_n - 1]) {
        return column[m_table_n - 1];
    }

    int lo = 0;
    int hi = m_table_n - 1;
    while (hi - lo > 1) {
        int mid = (lo + hi) / 2;
        if (pressure < m_table_p[mid]) {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    double dx = m_table_p[hi] - m_table_p[lo];
    if (fabs(dx) < 1.0e-12) {
        return column[lo];
    }
    return column[lo]
         + (column[hi] - column[lo]) * (pressure - m_table_p[lo]) / dx;
}

//----------------------------------------------------------------------
int FluidModel::last_error() const
{
    return F77_NAME(diag, DIAG).ierr;
}

//----------------------------------------------------------------------
const char* FluidModel::last_error_text() const
{
    int  code = 0;
    char buf[64];
    memset(buf, ' ', sizeof(buf));

    // PVTERR clears IERR as a side effect. The hidden length argument
    // is passed by value after the visible ones - see FortranBridge.hpp.
    F77_NAME(pvterr, PVTERR)(&code, buf, (int)sizeof(buf));

    int n = (int)sizeof(buf) - 1;
    while (n > 0 && buf[n - 1] == ' ') {
        n--;
    }
    memcpy(m_err_text, buf, n);
    m_err_text[n] = '\0';
    return m_err_text;
}
