from setuptools import Extension, setup

# A namespace package (PEP 420) in a src layout: src/nsx has no __init__.py,
# and the tests lie inside the package, as zope.hookable's do.
setup(
    name="nsx.calc",
    version="0",
    package_dir={"": "src"},
    packages=["nsx.calc", "nsx.calc.tests"],
    ext_modules=[Extension("nsx.calc._calc", sources=["src/nsx/calc/_calc.c"])],
)
