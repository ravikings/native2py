#pragma once

namespace common {

// Unit conversions shared across services — the kind of small, stable
// numerical helper that shouldn't be copy-pasted into every service.
double feet_to_metres(double feet);
double psi_to_pascal(double psi);
double fahrenheit_to_celsius(double fahrenheit);

}  // namespace common
