/* langadapter_c_counters: writes and zeroes the gcov counters of one loaded
   shared library from Python, without ctypes (a Python built without libffi
   has none). The C adapter compiles it against the test interpreter's
   headers, beside the pytest plugin that calls it. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <dlfcn.h>

static void (*dump_counters)(void) = NULL;
static void (*reset_counters)(void) = NULL;

static PyObject *bind(PyObject *self, PyObject *arg) {
    const char *path = PyUnicode_AsUTF8(arg);
    void *library;
    if (path == NULL) {
        return NULL;
    }
    library = dlopen(path, RTLD_NOW | RTLD_NOLOAD);
    if (library == NULL) {
        /* dlerror() may say nothing for a library that is not loaded. */
        const char *why = dlerror();
        PyErr_Format(PyExc_OSError, "%s is not loaded%s%s", path, why ? ": " : "",
                     why ? why : "");
        return NULL;
    }
    /* The coverage build links the adapter's exports of gcov's functions
       (gcov_exports.c), which a link that hides libgcov's keeps. */
    dump_counters = (void (*)(void))dlsym(library, "langadapter_c_gcov_dump");
    reset_counters = (void (*)(void))dlsym(library, "langadapter_c_gcov_reset");
    if (dump_counters == NULL || reset_counters == NULL) {
        PyErr_Format(PyExc_OSError, "%s exports no gcov dump and reset", path);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *flush(PyObject *self, PyObject *unused) {
    if (dump_counters == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "no library is bound");
        return NULL;
    }
    dump_counters();
    reset_counters();
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"bind", bind, METH_O, "bind(path): the loaded library whose counters flush writes"},
    {"flush", flush, METH_NOARGS, "flush(): write the counters, then zero them"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "langadapter_c_counters", NULL, -1, methods
};

PyMODINIT_FUNC PyInit_langadapter_c_counters(void) {
    return PyModule_Create(&module);
}
