#include "geometry.hpp"

#include <cmath>

#include "units.hpp"

double Geometry::circle_area(double radius) {
    return M_PI * radius * radius;
}

double Geometry::rectangle_area(double width, double height) {
    return width * height;
}

double Geometry::hypotenuse(double a, double b) {
    return std::sqrt(a * a + b * b);
}

double Geometry::circle_area_from_feet(double radius_feet) {
    return circle_area(common::feet_to_metres(radius_feet));
}
