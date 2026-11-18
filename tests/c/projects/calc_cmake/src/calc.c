#include "calc.h"
#include "calc_config.h"

long clamp_to_limit(long x) {
    if (x < 0) {
        return 0;
    }
    if (x > CALC_LIMIT) {
        return CALC_LIMIT;
    }
    return x;
}

int sign(long x) {
    return x > 0 ? 1 : (x < 0 ? -1 : 0);
}
