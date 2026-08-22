//======================================================================
// FluidModel.hpp - object wrapper over the PVTCOR correlation deck
//
// Created  09-Feb-1994  T. Reinholt
// Revised  17-Jun-1997  cached PVT table to avoid re-entering PVTSET
//                       200000 times per report step
// Revised  22-Oct-2001  const correctness pass
//
// IMPORTANT - there is exactly ONE fluid description in the Fortran
// layer (/FLUID/ in PETRO.INC). Constructing two FluidModel objects
// with different gravities does NOT give you two fluids: whichever
// one called activate() last wins, for the whole process. The
// s_active pointer below exists only to catch that mistake in debug
// builds. Fixing it properly means threading a handle through every
// routine in pvtcor.f and that has been deferred since 1994.
//======================================================================
#ifndef PETRO_FLUID_MODEL_HPP
#define PETRO_FLUID_MODEL_HPP

// Correlation families understood by PVTINI.
enum CorrelationFamily {
    CORR_STANDING     = 1,
    CORR_VAZQUEZ_BEGGS = 2,
    CORR_GLASO        = 3
};

// Plain struct mirroring /PVTOUT/. Returned by value from
// FluidModel::properties_at so callers do not read the COMMON block.
struct PvtState {
    double bo;
    double bg;
    double bw;
    double rs;
    double mu_oil;
    double mu_gas;
    double mu_water;
    double rho_oil;
    double rho_gas;
    double rho_water;
    double z_factor;
    double c_oil;
};

class FluidModel {
public:
    FluidModel(double api_gravity, double gas_gravity, double temp_f);
    FluidModel(double api_gravity, double gas_gravity, double temp_f,
               int correlation);
    ~FluidModel();

    // Push this object's description into /FLUID/. Called implicitly by
    // every accessor below; public because Simulator has to force it
    // after the deck reader has changed the active fluid.
    void activate() const;

    double api_gravity() const;
    double gas_gravity() const;
    double temperature() const;
    double bubble_point() const;

    // Single property accessors. Each one calls into pvtcor.f.
    double solution_gor(double pressure) const;
    double oil_fvf(double pressure) const;
    double oil_viscosity(double pressure) const;
    double gas_viscosity(double pressure) const;
    double gas_fvf(double pressure) const;
    double z_factor(double pressure) const;
    double oil_density(double pressure) const;

    // Whole state in one Fortran call. Prefer this in inner loops.
    PvtState properties_at(double pressure) const;

    // Precompute a pressure table between p_min and p_max with n rows.
    // Subsequent calls to the accessors interpolate instead of calling
    // Fortran. Returns 0 on success, non zero on a PVT error.
    int build_table(double p_min, double p_max, int n);
    int table_size() const;
    void clear_table();

    // Non zero if the last Fortran call set /DIAG/ IERR.
    int last_error() const;
    const char* last_error_text() const;

private:
    // 1994 codebase, pre-STL. Manual buffer, hand rolled copy ctor.
    FluidModel(const FluidModel& other);
    FluidModel& operator=(const FluidModel& other);

    double interpolate(const double* column, double pressure) const;

    double m_api;
    double m_gas_sg;
    double m_temp_f;
    int    m_correlation;
    mutable double m_bubble_point;

    int     m_table_n;
    double* m_table_p;
    double* m_table_bo;
    double* m_table_rs;
    double* m_table_mu;
    double* m_table_z;

    mutable char m_err_text[80];

    static const FluidModel* s_active;
};

#endif // PETRO_FLUID_MODEL_HPP
