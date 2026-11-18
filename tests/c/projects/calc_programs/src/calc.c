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

long clamp_twice(long x, long lo, long hi) {
    return clamp(twice(x), lo, hi);
}

int sign(long x) {
    return x > 0 ? 1 : (x < 0 ? -1 : 0);
}
