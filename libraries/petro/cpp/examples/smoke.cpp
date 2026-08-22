//======================================================================
// smoke.cpp - minimal end to end exercise of the petro library.
// Not a unit test. Prints values an engineer can eyeball against the
// 1993 reference manual tables.
//======================================================================
#include "FluidModel.hpp"
#include "WellModel.hpp"
#include "Simulator.hpp"
#include <stdio.h>

int main()
{
    FluidModel fluid(35.0, 0.65, 180.0, CORR_VAZQUEZ_BEGGS);
    printf("Pb        = %10.2f psia\n", fluid.bubble_point());
    printf("Rs(2000)  = %10.2f scf/stb\n", fluid.solution_gor(2000.0));
    printf("Bo(2000)  = %10.4f rb/stb\n",  fluid.oil_fvf(2000.0));
    printf("muo(2000) = %10.4f cp\n",      fluid.oil_viscosity(2000.0));
    printf("Z(2000)   = %10.4f\n",         fluid.z_factor(2000.0));

    Simulator sim;
    if (sim.initialize(10, 10, 3) != 0) {
        printf("initialize failed: %s\n", sim.last_error_text());
        return 1;
    }
    sim.set_fluid(&fluid);
    sim.set_uniform_porosity(0.22);
    sim.set_uniform_permeability(150.0, 150.0, 15.0);
    sim.set_corey_endpoints(0.20, 0.25, 0.05, 2.0, 2.0, 2.0);
    if (sim.compute_transmissibility() != 0) {
        printf("trncal failed: %s\n", sim.last_error_text());
        return 1;
    }
    if (sim.equilibrate(8200.0, 7900.0, 4000.0, 8000.0) != 0) {
        printf("equili failed: %s\n", sim.last_error_text());
        return 1;
    }
    printf("OIP       = %12.1f stb\n", sim.oil_in_place());

    WellModel well("PROD-01", WELL_PRODUCER_BHP);
    well.set_completion(0.354, 2.0);
    well.set_tubing(0.2010, 0.0006, 8000.0, 8400.0);
    well.set_wellhead_pressure(250.0);
    well.set_water_cut(0.15);
    well.set_gas_oil_ratio(600.0);
    well.set_reservoir(4000.0, 6000.0);
    if (sim.add_well(&well, 5, 5, 1, 3) <= 0) {
        printf("add_well failed: %s\n", sim.last_error_text());
        return 1;
    }
    printf("IPR@2500  = %10.2f stb/d\n", well.inflow_rate(2500.0));
    printf("TPC@1000  = %10.2f psia\n",  well.outflow_bhp(1000.0));

    OperatingPoint op = well.solve_operating_point();
    printf("nodal     = %10.2f stb/d @ %8.2f psia (iconv=%d)\n",
           op.liquid_rate, op.flowing_bhp, op.converged);

    if (sim.advance(1.0) != 0) {
        printf("advance failed: %s\n", sim.last_error_text());
        return 1;
    }
    printf("OIP+1d    = %12.1f stb\n", sim.oil_in_place());
    return 0;
}
