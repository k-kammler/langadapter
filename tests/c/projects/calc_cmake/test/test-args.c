#include <stdlib.h>

#include "calc.h"

/* A data-driven test program, as jansson's json_process: the input and the
   expected value come from the command line or the environment. */
int main(int argc, char **argv) {
    const char *expected = argc > 2 ? argv[2] : getenv("CALC_EXPECT");
    if (argc < 2 || expected == NULL) {
        return 2;
    }
    return clamp_to_limit(atol(argv[1])) == atol(expected) ? 0 : 1;
}
