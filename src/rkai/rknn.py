"""RKNN 2.3.2 Linux AArch64 C ABI; absolute managed library, no system replacement.

Structures mirror the fixed official rknn_api.h (see ROCKCHIP). Native execution
remains unverified until board tests. No Lite2 implicit /usr/lib lookup is used.
"""

import ctypes as c
import math

from .errors import RkaiError

MAX_DIMS = 16
MAX_NAME = 256
MAX_TENSORS = 64
MAX_OUTPUT_ELEMENTS = 16 * 1024 * 1024
QUERY_COUNTS, QUERY_INPUT, QUERY_OUTPUT, QUERY_VERSION = 0, 1, 2, 5
FLOAT32, INT32, INT64 = 0, 6, 8
NCHW, NHWC, UNDEFINED_LAYOUT = 0, 1, 2


class Counts(c.Structure):
    _fields_ = [("inputs", c.c_uint32), ("outputs", c.c_uint32)]


class Attribute(c.Structure):
    _fields_ = [
        ("index", c.c_uint32),
        ("n_dims", c.c_uint32),
        ("dims", c.c_uint32 * MAX_DIMS),
        ("name", c.c_char * MAX_NAME),
        ("n_elems", c.c_uint32),
        ("size", c.c_uint32),
        ("fmt", c.c_int),
        ("type", c.c_int),
        ("qnt_type", c.c_int),
        ("fl", c.c_int8),
        ("zp", c.c_int32),
        ("scale", c.c_float),
        ("w_stride", c.c_uint32),
        ("size_with_stride", c.c_uint32),
        ("pass_through", c.c_uint8),
        ("h_stride", c.c_uint32),
    ]


class Input(c.Structure):
    _fields_ = [
        ("index", c.c_uint32),
        ("buf", c.c_void_p),
        ("size", c.c_uint32),
        ("pass_through", c.c_uint8),
        ("type", c.c_int),
        ("fmt", c.c_int),
    ]


class Output(c.Structure):
    _fields_ = [
        ("want_float", c.c_uint8),
        ("is_prealloc", c.c_uint8),
        ("index", c.c_uint32),
        ("buf", c.c_void_p),
        ("size", c.c_uint32),
    ]


class Version(c.Structure):
    _fields_ = [("api", c.c_char * 256), ("driver", c.c_char * 256)]


class RknnBackend:
    def __init__(self, path, config, runtime, adapter=None):
        self.config = config
        self.context = c.c_uint64()
        self.lib = c.CDLL(str(runtime))
        signatures = {
            "rknn_init": [c.POINTER(c.c_uint64), c.c_void_p, c.c_uint32, c.c_uint32, c.c_void_p],
            "rknn_query": [c.c_uint64, c.c_int, c.c_void_p, c.c_uint32],
            "rknn_inputs_set": [c.c_uint64, c.c_uint32, c.POINTER(Input)],
            "rknn_run": [c.c_uint64, c.c_void_p],
            "rknn_outputs_get": [c.c_uint64, c.c_uint32, c.POINTER(Output), c.c_void_p],
            "rknn_outputs_release": [c.c_uint64, c.c_uint32, c.POINTER(Output)],
            "rknn_destroy": [c.c_uint64],
        }
        for name, args in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = args, c.c_int
        try:
            # size=0 requests official path loading; do not keep a second full model in RAM.
            encoded = str(path).encode()
            self.check(self.lib.rknn_init(c.byref(self.context), c.c_char_p(encoded), 0, 0, None))
            version = Version()
            self.query(QUERY_VERSION, version)
            api, driver = version.api.decode(), version.driver.decode()
            if not api.startswith("2.3.2") or not driver:
                raise RkaiError(
                    "runtime_version", "RKNN runtime/driver version is not established", 3
                )
            self.evidence = {"runtime": api, "driver": driver, "provider": "rockchip-npu"}
            counts = Counts()
            self.query(QUERY_COUNTS, counts)
            if not (0 < counts.inputs <= MAX_TENSORS and 0 < counts.outputs <= MAX_TENSORS):
                raise RkaiError("unsupported_tensor", "RKNN tensor count exceeds binding budget", 3)
            self.inputs = [self.attribute(QUERY_INPUT, i) for i in range(counts.inputs)]
            self.outputs = [self.attribute(QUERY_OUTPUT, i) for i in range(counts.outputs)]
            if len(config.input_names) != counts.inputs:
                raise RkaiError("invalid_backend", "Explicit RKNN input order is required", 2)
        except BaseException:
            self.close()
            raise

    def check(self, code):
        if code != 0:
            raise RkaiError("native_error", f"RKNN API returned {code}")

    def query(self, command, value):
        self.check(self.lib.rknn_query(self.context, command, c.byref(value), c.sizeof(value)))

    def attribute(self, command, index):
        value = Attribute(index=index)
        self.query(command, value)
        if not 0 < value.n_dims <= MAX_DIMS:
            raise RkaiError("unsupported_tensor", "Invalid RKNN dimensions", 3)
        shape = tuple(value.dims[: value.n_dims])
        if math.prod(shape) != value.n_elems or not 0 < value.n_elems <= MAX_OUTPUT_ELEMENTS:
            raise RkaiError("unsupported_tensor", "RKNN shape exceeds binding budget", 3)
        return value

    def infer(self, tensors):
        import numpy as np

        names = self.config.input_names
        if set(tensors) != set(names):
            raise RkaiError("invalid_input", "RKNN input names differ", 2)
        arrays = [np.ascontiguousarray(tensors[name]) for name in names]
        types = {np.dtype("float32"): FLOAT32, np.dtype("int32"): INT32, np.dtype("int64"): INT64}
        if any(array.dtype not in types for array in arrays):
            raise RkaiError(
                "unsupported_tensor", "RKNN binding accepts float32/int32/int64 inputs", 3
            )
        for array, attr in zip(arrays, self.inputs, strict=True):
            expected = tuple(attr.dims[: attr.n_dims])
            if array.ndim == 4:
                if attr.fmt == NHWC and self.config.input_layout == "nchw":
                    expected = (expected[0], expected[3], expected[1], expected[2])
                elif attr.fmt == NCHW and self.config.input_layout == "nhwc":
                    expected = (expected[0], expected[2], expected[3], expected[1])
            if array.shape != expected:
                raise RkaiError("invalid_input", "RKNN input shape differs from compiled graph", 2)
        inputs = (Input * len(arrays))(
            *[
                Input(
                    i,
                    a.ctypes.data,
                    a.nbytes,
                    0,
                    types[a.dtype],
                    (NCHW if self.config.input_layout == "nchw" else NHWC)
                    if a.ndim == 4
                    else UNDEFINED_LAYOUT,
                )
                for i, a in enumerate(arrays)
            ]
        )
        self.check(self.lib.rknn_inputs_set(self.context, len(arrays), inputs))
        self.check(self.lib.rknn_run(self.context, None))
        outputs = (Output * len(self.outputs))(
            *[Output(1, 0, i, None, 0) for i in range(len(self.outputs))]
        )
        self.check(self.lib.rknn_outputs_get(self.context, len(outputs), outputs, None))
        try:
            result = []
            for out, attr in zip(outputs, self.outputs, strict=True):
                if not out.buf or out.size != attr.n_elems * c.sizeof(c.c_float):
                    raise RkaiError("invalid_output", "RKNN output buffer size differs")
                data = np.ctypeslib.as_array(
                    c.cast(out.buf, c.POINTER(c.c_float)), shape=(attr.n_elems,)
                ).copy()
                result.append(data.reshape(tuple(attr.dims[: attr.n_dims])))
            return result
        finally:
            self.check(self.lib.rknn_outputs_release(self.context, len(outputs), outputs))

    def close(self):
        if self.context.value:
            self.check(self.lib.rknn_destroy(self.context))
            self.context = c.c_uint64()
