//======================================================================
// Simulator.cpp
//
// Created  12-Jan-1996  R. Salcedo
// Revised  19-Mar-2000  report step scheduling
// Revised  08-Jun-2004  rate accumulators
//======================================================================
#include "Simulator.hpp"
#include "FluidModel.hpp"
#include "WellModel.hpp"
#include "DeckReader.hpp"
#include "FortranBridge.hpp"

#include <string.h>
#include <stdio.h>
#include <math.h>

int Simulator::s_instances = 0;

// IPROP codes accepted by GRDSET. Keep in step with simcor.f.
static const int PROP_PORO   = 1;
static const int PROP_PERMX  = 2;
static const int PROP_PERMY  = 3;
static const int PROP_PERMZ  = 4;
static const int PROP_DZ     = 5;
static const int PROP_TOPS   = 6;
static const int PROP_NTG    = 7;
static const int PROP_ACTNUM = 8;

//----------------------------------------------------------------------
Simulator::Simulator()
{
    m_nx = 0;
    m_ny = 0;
    m_nz = 0;
    m_wells = 0;
    m_initialized = 0;
    m_transmissibility_built = 0;
    m_dt_min = 1.0e-3;
    m_dt_max = 30.0;
    m_fluid  = 0;
    m_deck   = 0;
    m_error[0] = '\0';

    s_instances++;
    // See the header. A second instance aliases the first one's grid.
}

//----------------------------------------------------------------------
Simulator::~Simulator()
{
    delete m_deck;
    m_deck = 0;
    s_instances--;
}

//----------------------------------------------------------------------
int Simulator::initialize(int nx, int ny, int nz)
{
    if (nx < 1 || ny < 1 || nz < 1) {
        strcpy(m_error, "grid dimensions must all be positive");
        return -1;
    }

    int mx = nx;
    int my = ny;
    int mz = nz;
    F77_NAME(simini, SIMINI)(&mx, &my, &mz);

    if (F77_NAME(diag, DIAG).ierr != 0) {
        sprintf(m_error, "SIMINI failed with IERR = %d",
                F77_NAME(diag, DIAG).ierr);
        F77_NAME(diag, DIAG).ierr = 0;
        return -1;
    }

    m_nx = nx;
    m_ny = ny;
    m_nz = nz;
    m_initialized = 1;
    m_transmissibility_built = 0;
    return 0;
}

//----------------------------------------------------------------------
int Simulator::initialize_from_deck(const char* filename)
{
    delete m_deck;
    m_deck = new DeckReader();

    int line = m_deck->read(filename);
    if (line != 0) {
        sprintf(m_error, "deck error at line %d: %.80s",
                line, m_deck->error_text());
        return -1;
    }
    if (!m_deck->has_dimensions()) {
        strcpy(m_error, "deck has no RUNSPEC DIMENS card");
        return -1;
    }

    // The deck reader has already pushed GRID and PROPS data into the
    // Fortran layer as it parsed, so all that is left here is to
    // record the dimensions and build transmissibilities.
    m_nx = m_deck->nx();
    m_ny = m_deck->ny();
    m_nz = m_deck->nz();
    m_wells = m_deck->well_count();
    m_initialized = 1;

    return compute_transmissibility();
}

//----------------------------------------------------------------------
int Simulator::nx() const { return m_nx; }
int Simulator::ny() const { return m_ny; }
int Simulator::nz() const { return m_nz; }
int Simulator::cell_count() const { return m_nx * m_ny * m_nz; }

//----------------------------------------------------------------------
int Simulator::push_array(int property_code, double* values, int n)
{
    if (!m_initialized) {
        strcpy(m_error, "initialize() must be called first");
        return -1;
    }
    if (values == 0 || n != cell_count()) {
        sprintf(m_error, "expected %d values, got %d", cell_count(), n);
        return -1;
    }

    int code  = property_code;
    int count = n;
    F77_NAME(grdset, GRDSET)(&code, values, &count);

    if (F77_NAME(diag, DIAG).ierr != 0) {
        sprintf(m_error, "GRDSET(%d) failed with IERR = %d",
                property_code, F77_NAME(diag, DIAG).ierr);
        F77_NAME(diag, DIAG).ierr = 0;
        return -1;
    }
    m_transmissibility_built = 0;
    return 0;
}

int Simulator::set_porosity(double* v, int n)
{ return push_array(PROP_PORO, v, n); }

int Simulator::set_permeability_x(double* v, int n)
{ return push_array(PROP_PERMX, v, n); }

int Simulator::set_permeability_y(double* v, int n)
{ return push_array(PROP_PERMY, v, n); }

int Simulator::set_permeability_z(double* v, int n)
{ return push_array(PROP_PERMZ, v, n); }

int Simulator::set_cell_thickness(double* v, int n)
{ return push_array(PROP_DZ, v, n); }

int Simulator::set_tops(double* v, int n)
{ return push_array(PROP_TOPS, v, n); }

int Simulator::set_active(double* v, int n)
{ return push_array(PROP_ACTNUM, v, n); }

//----------------------------------------------------------------------
int Simulator::set_uniform_porosity(double value)
{
    int n = cell_count();
    if (n <= 0) {
        strcpy(m_error, "initialize() must be called first");
        return -1;
    }
    double* buf = new double[n];
    for (int i = 0; i < n; i++) {
        buf[i] = value;
    }
    int rc = push_array(PROP_PORO, buf, n);
    delete[] buf;
    return rc;
}

//----------------------------------------------------------------------
int Simulator::set_uniform_permeability(double kx, double ky, double kz)
{
    int n = cell_count();
    if (n <= 0) {
        strcpy(m_error, "initialize() must be called first");
        return -1;
    }
    double* buf = new double[n];
    int rc = 0;

    for (int i = 0; i < n; i++) buf[i] = kx;
    rc = push_array(PROP_PERMX, buf, n);

    if (rc == 0) {
        for (int i = 0; i < n; i++) buf[i] = ky;
        rc = push_array(PROP_PERMY, buf, n);
    }
    if (rc == 0) {
        for (int i = 0; i < n; i++) buf[i] = kz;
        rc = push_array(PROP_PERMZ, buf, n);
    }
    delete[] buf;
    return rc;
}

//----------------------------------------------------------------------
int Simulator::compute_transmissibility()
{
    if (!m_initialized) {
        strcpy(m_error, "initialize() must be called first");
        return -1;
    }
    F77_NAME(trncal, TRNCAL)();
    if (F77_NAME(diag, DIAG).ierr != 0) {
        sprintf(m_error, "TRNCAL failed with IERR = %d",
                F77_NAME(diag, DIAG).ierr);
        F77_NAME(diag, DIAG).ierr = 0;
        return -1;
    }
    m_transmissibility_built = 1;
    return 0;
}

//----------------------------------------------------------------------
int Simulator::equilibrate(double woc_depth, double goc_depth,
                           double datum_pressure, double datum_depth)
{
    if (!m_transmissibility_built) {
        // Not strictly required by EQUILI, but running without it means
        // the first STEP call solves against a zero matrix and reports
        // spurious convergence. Caught the hard way, 14-Feb-1997.
        strcpy(m_error, "compute_transmissibility() must be called first");
        return -1;
    }
    double woc  = woc_depth;
    double goc  = goc_depth;
    double pdat = datum_pressure;
    double ddat = datum_depth;

    F77_NAME(equili, EQUILI)(&woc, &goc, &pdat, &ddat);

    if (F77_NAME(diag, DIAG).ierr != 0) {
        sprintf(m_error, "EQUILI failed with IERR = %d",
                F77_NAME(diag, DIAG).ierr);
        F77_NAME(diag, DIAG).ierr = 0;
        return -1;
    }
    return 0;
}

//----------------------------------------------------------------------
int Simulator::set_corey_endpoints(double swc, double sor, double sgc,
                                   double nw, double no, double ng)
{
    double a = swc, b = sor, c = sgc;
    double d = nw,  e = no,  f = ng;
    double krwm = 0.3, krom = 1.0, krgm = 0.8;

    F77_NAME(krini, KRINI)(&a, &b, &c, &d, &e, &f,
                           &krwm, &krom, &krgm);

    if (F77_NAME(diag, DIAG).ierr != 0) {
        sprintf(m_error, "KRINI failed with IERR = %d",
                F77_NAME(diag, DIAG).ierr);
        F77_NAME(diag, DIAG).ierr = 0;
        return -1;
    }
    return 0;
}

//----------------------------------------------------------------------
void Simulator::set_fluid(FluidModel* fluid)
{
    m_fluid = fluid;
    if (m_fluid != 0) {
        m_fluid->activate();
    }
}

FluidModel* Simulator::fluid() const { return m_fluid; }

//----------------------------------------------------------------------
int Simulator::add_well(WellModel* well, int i, int j, int k1, int k2)
{
    if (well == 0) {
        strcpy(m_error, "null well");
        return -1;
    }
    if (!m_initialized) {
        strcpy(m_error, "initialize() must be called first");
        return -1;
    }
    int index = well->attach_to_grid(i, j, k1, k2);
    if (index <= 0) {
        sprintf(m_error, "well '%.8s' could not be attached", well->name());
        return -1;
    }
    m_wells++;
    return index;
}

int Simulator::well_count() const { return m_wells; }

//----------------------------------------------------------------------
void Simulator::set_timestep_limits(double dt_min, double dt_max)
{
    if (dt_min > 0.0) m_dt_min = dt_min;
    if (dt_max > dt_min) m_dt_max = dt_max;
}

//----------------------------------------------------------------------
int Simulator::advance(double dt_days)
{
    if (!m_transmissibility_built) {
        strcpy(m_error, "compute_transmissibility() must be called first");
        return -1;
    }
    double dtin  = dt_days;
    double dtout = 0.0;
    int    iconv = 0;

    F77_NAME(step, STEP)(&dtin, &dtout, &iconv);

    if (iconv != 0) {
        sprintf(m_error, "timestep failed to converge at %.3f days",
                time_days());
        return -1;
    }
    return 0;
}

//----------------------------------------------------------------------
int Simulator::advance_to(double target_days)
{
    // 19-MAR-2000: the report step is honoured exactly; the internal
    // step is whatever STEP returns, clipped so we land on the report
    // time rather than stepping past it.
    int guard = 0;
    double dt = m_dt_max;

    while (time_days() < target_days - 1.0e-9) {
        double remaining = target_days - time_days();
        if (dt > remaining) {
            dt = remaining;
        }
        if (advance(dt) != 0) {
            return -1;
        }
        if (++guard > 100000) {
            strcpy(m_error, "advance_to exceeded 100000 timesteps");
            return -1;
        }
        dt = m_dt_max;
    }
    return 0;
}

//----------------------------------------------------------------------
double Simulator::time_days() const
{
    // TIME lives in /TSTEP/, which is not declared in FortranBridge.hpp
    // because MXCELL sized arrays precede it in GRID.INC and getting
    // the offsets wrong is fatal. Tracked separately instead.
    return 0.0;
}

double Simulator::oil_in_place() const
{
    return F77_NAME(fipoil, FIPOIL)();
}

double Simulator::average_pressure() const
{
    // Pore volume weighted average. Needs a Fortran accessor; the
    // 2004 reporter computed it here from cell_pressure and was
    // unusably slow on the 40000 cell decks.
    return 0.0;
}

//----------------------------------------------------------------------
RunState Simulator::state() const
{
    RunState s;
    s.time_days        = time_days();
    s.timestep_days    = m_dt_max;
    s.oil_in_place     = oil_in_place();
    s.average_pressure = average_pressure();
    s.steps_taken      = 0;
    s.cuts_taken       = 0;
    s.converged        = (F77_NAME(diag, DIAG).ierr == 0) ? 1 : 0;
    return s;
}

//----------------------------------------------------------------------
int Simulator::cell_index(int i, int j, int k) const
{
    if (i < 1 || i > m_nx || j < 1 || j > m_ny || k < 1 || k > m_nz) {
        return -1;
    }
    return (i - 1) + (j - 1) * m_nx + (k - 1) * m_nx * m_ny;
}

double Simulator::cell_pressure(int i, int j, int k) const
{
    // /STATE/ is not exposed through the bridge for the same offset
    // reason as /TSTEP/. Needs an accessor subroutine in simcor.f.
    if (cell_index(i, j, k) < 0) {
        return 0.0;
    }
    return 0.0;
}

double Simulator::cell_water_saturation(int i, int j, int k) const
{
    if (cell_index(i, j, k) < 0) {
        return 0.0;
    }
    return 0.0;
}

double Simulator::cell_gas_saturation(int i, int j, int k) const
{
    if (cell_index(i, j, k) < 0) {
        return 0.0;
    }
    return 0.0;
}

//----------------------------------------------------------------------
int Simulator::last_error() const
{
    return F77_NAME(diag, DIAG).ierr;
}

const char* Simulator::last_error_text() const
{
    return m_error;
}
