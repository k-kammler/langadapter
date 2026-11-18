from setuptools import Extension, setup

setup(
    name="calcext",
    version="0",
    packages=["calcext"],
    ext_modules=[Extension("calcext._calc", sources=["calcext/_calc.c"])],
)
