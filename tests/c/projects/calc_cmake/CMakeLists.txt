cmake_minimum_required(VERSION 3.10)
project(calc C)

# The target needs a header the configure step writes, as jansson's does.
set(CALC_LIMIT 10 CACHE STRING "the clamp's upper limit")
configure_file(src/calc_config.h.in ${CMAKE_CURRENT_BINARY_DIR}/include/calc_config.h)
include_directories(${CMAKE_CURRENT_BINARY_DIR}/include)

add_library(calc STATIC src/calc.c)
target_include_directories(calc PUBLIC src)

add_executable(test-calc test/test-calc.c)
target_link_libraries(test-calc calc)

add_executable(test-args test/test-args.c)
target_link_libraries(test-args calc)

enable_testing()
add_test(NAME own_program COMMAND test-calc)
add_test(NAME low COMMAND test-args -5 0)
add_test(NAME high COMMAND test-args 50 10)
add_test(NAME wrong_on_purpose COMMAND test-args 5 6)
set_tests_properties(wrong_on_purpose PROPERTIES WILL_FAIL TRUE)
add_test(NAME from_environment COMMAND test-args 50)
set_tests_properties(from_environment PROPERTIES ENVIRONMENT "CALC_EXPECT=10")
add_test(NAME a_script COMMAND sh -c "exit 0")
