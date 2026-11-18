#include <assert.h>

#include "calc.h"

int main(void) {
    assert(clamp_to_limit(-5) == 0);
    assert(clamp_to_limit(5) == 5);
    return 0;
}
