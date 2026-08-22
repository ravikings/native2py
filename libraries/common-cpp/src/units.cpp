#include "units.hpp"

namespace common {

double feet_to_metres(double feet) {
    return feet * 0.3048;
}

double psi_to_pascal(double psi) {
    return psi * 6894.757293168361;
}

double fahrenheit_to_celsius(double fahrenheit) {
    return (fahrenheit - 32.0) * 5.0 / 9.0;
}

}  // namespace common
