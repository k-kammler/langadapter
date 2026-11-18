"""clamp, from the C extension when it loads, else in Python."""
try:
    from nsx.calc._calc import clamp
except ImportError:
    def clamp(x, lo, hi):
        return lo if x < lo else hi if x > hi else x
