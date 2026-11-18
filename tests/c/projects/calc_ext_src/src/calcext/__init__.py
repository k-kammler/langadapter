"""clamp and sign, from the C extension when it loads, else in Python."""
try:
    from calcext._calc import clamp, sign
except ImportError:
    def clamp(x, lo, hi):
        return lo if x < lo else hi if x > hi else x

    def sign(x):
        return (x > 0) - (x < 0)
