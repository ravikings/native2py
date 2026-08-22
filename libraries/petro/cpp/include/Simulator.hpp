//======================================================================
// Simulator.hpp - driver facade over SIMCOR
//
// Created  12-Jan-1996  R. Salcedo
// Revised  19-Mar-2000  report step scheduling separated from time step
// Revised  08-Jun-2004  added rate accumulators for the web reporter
//
// One Simulator per process. The Fortran layer keeps the grid, the
// solution arrays and the well list in COMMON (see GRID.INC), so a
// second instance shares all of it. The constructor asserts on the
// second construction in debug builds and silently aliases in
// release builds, which is how the 1998 dual-realization run
// produced two identical outputs.
//======================================================================
#ifndef PETRO_SIMULATOR_HPP
#define PETRO_SIMULATOR_HPP

class FluidModel;
class WellModel;
class DeckReader;

// Snapshot of the run state after a report step.
struct RunState {
    double time_days;
    double timestep_days;
    double oil_in_place;
    double average_pressure;
    int    steps_taken;
    int    cuts_taken;
    int    converged;
};

class Simulator {
public:
    Simulator();
    ~Simulator();

    // Build the grid and push static properties into the Fortran layer.
    // Must be called before anything else. Returns 0 on success.
    int initialize(int nx, int ny, int nz);

    // Load a deck and initialize from it in one step.
    int initialize_from_deck(const char* filename);

    int nx() const;
    int ny() const;
    int nz() const;
    int cell_count() const;

    // Static property loading. Each pushes one array through GRDSET.
    // values must hold exactly cell_count() entries in natural order.
    int set_porosity(double* values, int n);
    int set_permeability_x(double* values, int n);
    int set_permeability_y(double* values, int n);
    int set_permeability_z(double* values, int n);
    int set_cell_thickness(double* values, int n);
    int set_tops(double* values, int n);
    int set_active(double* values, int n);

    // Uniform shortcuts for quick studies.
    int set_uniform_porosity(double value);
    int set_uniform_permeability(double kx, double ky, double kz);

    // Build transmissibilities. Call after ALL property loads.
    int compute_transmissibility();

    // Gravity capillary equilibrium.
    int equilibrate(double woc_depth, double goc_depth,
                    double datum_pressure, double datum_depth);

    // Relative permeability end points (forwarded to KRINI).
    int set_corey_endpoints(double swc, double sor, double sgc,
                            double nw, double no, double ng);

    // Fluid description. The simulator does not own the model.
    void set_fluid(FluidModel* fluid);
    FluidModel* fluid() const;

    // Well registration.
    int add_well(WellModel* well, int i, int j, int k1, int k2);
    int well_count() const;

    // Time stepping.
    void set_timestep_limits(double dt_min, double dt_max);
    int advance(double dt_days);
    int advance_to(double target_days);

    RunState state() const;

    double time_days() const;
    double oil_in_place() const;
    double average_pressure() const;

    // Cell level queries, 1 based i, j, k to match the deck.
    double cell_pressure(int i, int j, int k) const;
    double cell_water_saturation(int i, int j, int k) const;
    double cell_gas_saturation(int i, int j, int k) const;

    int last_error() const;
    const char* last_error_text() const;

private:
    Simulator(const Simulator& other);
    Simulator& operator=(const Simulator& other);

    int push_array(int property_code, double* values, int n);
    int cell_index(int i, int j, int k) const;

    int m_nx;
    int m_ny;
    int m_nz;
    int m_wells;
    int m_initialized;
    int m_transmissibility_built;

    double m_dt_min;
    double m_dt_max;

    FluidModel* m_fluid;
    DeckReader* m_deck;

    mutable char m_error[128];

    static int s_instances;
};

#endif // PETRO_SIMULATOR_HPP
