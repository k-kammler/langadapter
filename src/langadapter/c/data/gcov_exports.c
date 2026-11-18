/* gcov's dump and reset under the C adapter's own names, for the coverage
   build of a Python extension. The adapter compiles this file once and links
   its object into that build. libgcov is an archive, and a link that keeps
   an archive's symbols local, such as python-build-standalone's
   -Wl,--exclude-libs,ALL, leaves __gcov_dump and __gcov_reset out of the
   extension's exported symbols. These functions live in an object of their
   own, so they stay exported, and the counters helper finds them. */
extern void __gcov_dump(void);
extern void __gcov_reset(void);

void langadapter_c_gcov_dump(void) {
    __gcov_dump();
}

void langadapter_c_gcov_reset(void) {
    __gcov_reset();
}
