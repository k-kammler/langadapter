#include <math.h>

#include "calc.h"
#include "util.h"

long clamp(long x, long lo, long hi) {
    if (x < lo) {
        return lo;
    }
    if (x > hi) {
        return hi;
    }
    return x;
}

int sign(long x) {
    return x > 0 ? 1 : (x < 0 ? -1 : 0);
}

double hypotenuse(double a, double b) {
    return sqrt(square(a) + square(b));
}

#if 0
static int never_compiled(int x) {
    return x + 1;
}
#endif

long span(const char *first, const char *last) {
    return last - first;
}
