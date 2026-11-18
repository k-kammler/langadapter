#include <assert.h>

#include "calc.h"
#include "framework.h"

static void test_clamp(void) {
    assert(clamp(-5, 0, 10) == 0);
    assert(clamp(5, 0, 10) == 5);
}

static void test_clamp_twice(void) {
    assert(clamp_twice(4, 0, 10) == 8);
}

static UnitTestFunction tests[] = {test_clamp, test_clamp_twice, 0};

int main(void) {
    run_tests(tests);
    return 0;
}
