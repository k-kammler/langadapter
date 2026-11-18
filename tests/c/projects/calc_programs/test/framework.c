#include "framework.h"

void run_tests(UnitTestFunction *tests) {
    for (int i = 0; tests[i] != 0; ++i) {
        tests[i]();
    }
}
