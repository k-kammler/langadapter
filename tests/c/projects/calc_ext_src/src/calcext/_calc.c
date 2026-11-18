#define PY_SSIZE_T_CLEAN
#include <Python.h>

static long clamp(long x, long lo, long hi) {
    if (x < lo) {
        return lo;
    }
    if (x > hi) {
        return hi;
    }
    return x;
}

static PyObject *calc_clamp(PyObject *self, PyObject *args) {
    long x, lo, hi;
    if (!PyArg_ParseTuple(args, "lll", &x, &lo, &hi)) {
        return NULL;
    }
    return PyLong_FromLong(clamp(x, lo, hi));
}

static PyObject *calc_sign(PyObject *self, PyObject *arg) {
    long x = PyLong_AsLong(arg);
    if (x == -1 && PyErr_Occurred()) {
        return NULL;
    }
    return PyLong_FromLong(x > 0 ? 1 : (x < 0 ? -1 : 0));
}

static PyObject *calc_head(PyObject *self, PyObject *arg) {
    static const long values[] = {7, 8, 9};
    /* volatile: the mutant table != NULL reads table[0] of a NULL table at any
       optimization level. */
    const long *volatile table = PyObject_IsTrue(arg) ? values : NULL;
    if (table == NULL) {
        return PyLong_FromLong(-1);
    }
    return PyLong_FromLong(table[0]);
}

static PyMethodDef methods[] = {
    {"clamp", calc_clamp, METH_VARARGS, "clamp(x, lo, hi)"},
    {"sign", calc_sign, METH_O, "sign(x)"},
    {"head", calc_head, METH_O, "head(flag)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_calc", NULL, -1, methods};

PyMODINIT_FUNC PyInit__calc(void) {
    PyObject *made = PyModule_Create(&module);
    if (made == NULL) {
        return NULL;
    }
    return made;
}
