#include "geometry.hpp"
#include <cmath>

double Geometry::circle_area(double radius) {
    return M_PI * radius * radius;
}

double Geometry::hypotenuse(double a, double b) {
    return std::sqrt(a * a + b * b);
}
