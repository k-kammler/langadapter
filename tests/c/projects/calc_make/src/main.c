#include <stdio.h>

#include "calc.h"

int main(void) {
    printf("%ld\n", clamp(12, 0, 10));
    return 0;
}
