from setuptools import Extension, setup

# A src layout: the package lies in src/, which a test run must import from.
setup(
    name="calcext",
    version="0",
    package_dir={"": "src"},
    packages=["calcext"],
    ext_modules=[Extension("calcext._calc", sources=["src/calcext/_calc.c"])],
)
