class Geometry {
public:
    double circle_area(double radius);
    double rectangle_area(double width, double height);
    double hypotenuse(double a, double b);

    // Uses the shared common-cpp unit conversions.
    double circle_area_from_feet(double radius_feet);
};
