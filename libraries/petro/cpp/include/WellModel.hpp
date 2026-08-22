//======================================================================
// WellModel.hpp - single well inflow / outflow and nodal analysis
//
// Created  17-May-1995  R. Salcedo
// Revised  02-Sep-1999  deviated well traverse (TVD != MD)
// Revised  11-Apr-2002  lift curve caching for the surface network
//
// Sits on top of WELLIB (inflow, Peaceman WI, nodal) and HYDRAU
// (tubing traverse), both of which sit on PVTCOR. Constructing a
// WellModel does NOT register the well with the simulator - call
// attach_to_grid() for that, and only after Simulator::initialize().
//======================================================================
#ifndef PETRO_WELL_MODEL_HPP
#define PETRO_WELL_MODEL_HPP

class FluidModel;

enum WellControl {
    WELL_PRODUCER_BHP  = 1,
    WELL_PRODUCER_RATE = 2,
    WELL_INJECTOR_BHP  = 3,
    WELL_INJECTOR_RATE = 4
};

// Result of a nodal analysis solve.
struct OperatingPoint {
    double liquid_rate;      // stb/d
    double flowing_bhp;      // psia
    int    converged;        // 0 = ok, 1 = iteration limit, 2 = no crossing
};

class WellModel {
public:
    WellModel(const char* name, int control);
    ~WellModel();

    const char* name() const;
    int control() const;
    void set_control(int control);

    // Completion geometry.
    void set_completion(double radius_ft, double skin);
    void set_tubing(double id_ft, double roughness_ft,
                    double tvd_ft, double md_ft);
    void set_wellhead_pressure(double psia);

    // Fluid split at surface conditions.
    void set_water_cut(double fraction);
    void set_gas_oil_ratio(double scf_per_stb);

    double radius() const;
    double skin() const;
    double tubing_id() const;
    double water_cut() const;
    double gas_oil_ratio() const;

    // Reservoir side. q_max is the absolute open flow potential.
    void set_reservoir(double avg_pressure, double q_max);

    // Inflow rate at a given flowing bottomhole pressure, stb/d.
    double inflow_rate(double flowing_bhp) const;

    // Outflow: bottomhole pressure required to lift a given rate.
    double outflow_bhp(double liquid_rate) const;

    // Intersect the two curves. Wraps NODAL.
    OperatingPoint solve_operating_point() const;

    // Register this well with the active simulator grid at column
    // (i, j), perforated from layer k1 to k2 inclusive. Returns the
    // Fortran well index, or 0 on failure.
    int attach_to_grid(int i, int j, int k1, int k2);
    int fortran_index() const;

    // Productivity index of one perforation, from Peaceman. Only
    // valid after attach_to_grid.
    double well_index(int perforation) const;
    int perforation_count() const;

private:
    WellModel(const WellModel& other);
    WellModel& operator=(const WellModel& other);

    char   m_name[9];        // Fortran CHARACTER*8 plus terminator
    int    m_control;
    int    m_fortran_index;
    int    m_perfs;

    double m_radius;
    double m_skin;
    double m_tubing_id;
    double m_roughness;
    double m_tvd;
    double m_md;
    double m_wellhead_p;
    double m_water_cut;
    double m_gor;
    double m_res_pressure;
    double m_q_max;
};

#endif // PETRO_WELL_MODEL_HPP
