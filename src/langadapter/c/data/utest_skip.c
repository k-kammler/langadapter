/* utest.h's test filter for the program that a crash restarts: the tests that
   LANGADAPTER_C_SKIP_TESTS names, one per line, are left out, and every other
   test runs. It replaces utest.h's weak definition in that program only. The
   program runs without --filter, so the filter argument is not read. */
#include <stdlib.h>
#include <string.h>

int utest_should_filter_test(const char *filter, const char *testcase) {
  const char *line = getenv("LANGADAPTER_C_SKIP_TESTS");
  size_t length = strlen(testcase);
  (void)filter;
  while (line && '\0' != *line) {
    const char *end = strchr(line, '\n');
    size_t size = end ? (size_t)(end - line) : strlen(line);
    if (size == length && 0 == strncmp(line, testcase, length)) {
      return 1;
    }
    line = end ? end + 1 : NULL;
  }
  return 0;
}
