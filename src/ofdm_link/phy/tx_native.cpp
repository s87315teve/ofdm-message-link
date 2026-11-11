// Native transmit encode steps that held the GIL in the duplex engine: rate
// matching (a gather through the plan's read index), symbol mapping and OFDM
// modulation (pilot insertion, 64-point unitary IFFT, cyclic prefix).  Each
// releases the GIL while it computes.  The Python implementations
// (rate_match.rate_match, codec.map_symbols, ofdm.modulate_ofdm) stay the
// reference and the fallback (ofdm_link.phy.native_backend).
//
// Rate matching and mapping are exact (an index gather; levels divided by the
// same double scale and rounded to float32 as NumPy does).  The IFFT runs in
// double and rounds once to complex64, so it differs from NumPy's
// single-precision transform by float32 rounding only.

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr int kFft = 64;

struct C {
    double re;
    double im;
};

inline C cadd(C a, C b) { return {a.re + b.re, a.im + b.im}; }
inline C csub(C a, C b) { return {a.re - b.re, a.im - b.im}; }
inline C cmul(C a, C b) { return {a.re * b.re - a.im * b.im, a.re * b.im + a.im * b.re}; }

class AllowThreads {
public:
    AllowThreads() : state_(PyEval_SaveThread()) {}
    ~AllowThreads() { PyEval_RestoreThread(state_); }
    AllowThreads(const AllowThreads&) = delete;
    AllowThreads& operator=(const AllowThreads&) = delete;

private:
    PyThreadState* state_;
};

class Buffer {
public:
    Buffer(PyObject* object, const char* name) {
        if (PyObject_GetBuffer(object, &view_, PyBUF_CONTIG_RO) != 0) {
            throw std::runtime_error(std::string(name) + " must be a contiguous buffer");
        }
        held_ = true;
    }
    ~Buffer() {
        if (held_) PyBuffer_Release(&view_);
    }
    Buffer(const Buffer&) = delete;
    Buffer& operator=(const Buffer&) = delete;
    const void* data() const { return view_.buf; }
    Py_ssize_t bytes() const { return view_.len; }

private:
    Py_buffer view_{};
    bool held_ = false;
};

class Output {
public:
    Output(PyObject* object, const char* name, size_t bytes) {
        if (PyObject_GetBuffer(object, &view_, PyBUF_CONTIG | PyBUF_WRITABLE) != 0) {
            throw std::runtime_error(std::string(name) + " must be a writable contiguous buffer");
        }
        held_ = true;
        if (static_cast<size_t>(view_.len) != bytes) {
            throw std::runtime_error(std::string(name) + " has the wrong size");
        }
    }
    ~Output() {
        if (held_) PyBuffer_Release(&view_);
    }
    Output(const Output&) = delete;
    Output& operator=(const Output&) = delete;
    void* data() { return view_.buf; }

private:
    Py_buffer view_{};
    bool held_ = false;
};

// Inverse unitary FFT of 64 points (norm="ortho").
void ifft64(const C* input, C* output) {
    static const struct Table {
        int reverse[kFft];
        C twiddle[kFft / 2];
        Table() {
            for (int i = 0; i < kFft; ++i) {
                int r = 0;
                for (int bit = 0; bit < 6; ++bit) r |= ((i >> bit) & 1) << (5 - bit);
                reverse[i] = r;
            }
            for (int k = 0; k < kFft / 2; ++k) {
                twiddle[k] = {std::cos(2.0 * kPi * k / kFft), std::sin(2.0 * kPi * k / kFft)};
            }
        }
    } table;
    for (int i = 0; i < kFft; ++i) output[table.reverse[i]] = input[i];
    for (int size = 2; size <= kFft; size *= 2) {
        const int half = size / 2;
        const int stride = kFft / size;
        for (int start = 0; start < kFft; start += size) {
            for (int k = 0; k < half; ++k) {
                const C t = cmul(table.twiddle[k * stride], output[start + k + half]);
                const C u = output[start + k];
                output[start + k] = cadd(u, t);
                output[start + k + half] = csub(u, t);
            }
        }
    }
    for (int i = 0; i < kFft; ++i) output[i] = {output[i].re * 0.125, output[i].im * 0.125};
}

// gather(values: uint8 buffer, index: int32 buffer, out: uint8 buffer): out = values[index]
PyObject* native_gather(PyObject*, PyObject* args) {
    PyObject* values_object = nullptr;
    PyObject* index_object = nullptr;
    PyObject* out_object = nullptr;
    if (!PyArg_ParseTuple(args, "OOO", &values_object, &index_object, &out_object)) {
        return nullptr;
    }
    try {
        Buffer values(values_object, "values");
        Buffer index(index_object, "index");
        if (index.bytes() % static_cast<Py_ssize_t>(sizeof(int32_t)) != 0) {
            throw std::runtime_error("index must be int32");
        }
        const size_t count = static_cast<size_t>(index.bytes()) / sizeof(int32_t);
        const size_t size = static_cast<size_t>(values.bytes());
        Output target(out_object, "out", count);
        auto* out = static_cast<uint8_t*>(target.data());
        bool in_range = true;
        {
            AllowThreads unlocked;
            const auto* source = static_cast<const uint8_t*>(values.data());
            const auto* positions = static_cast<const int32_t*>(index.data());
            for (size_t k = 0; k < count; ++k) {
                const int32_t position = positions[k];
                in_range = in_range && position >= 0 && static_cast<size_t>(position) < size;
                out[k] = in_range ? source[position] : 0;
            }
        }
        if (!in_range) throw std::runtime_error("index out of range");
        Py_RETURN_NONE;
    } catch (const std::exception& error) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
}

// map_bits(bits: uint8, axis_bits, levels: float32 x 2^axis_bits, scale, out: complex64).
// Each symbol takes axis_bits bits for I then axis_bits for Q, MSB first, and
// is levels[i] / scale + 1j levels[q] / scale (QPSK: levels (1, -1)).
PyObject* native_map_bits(PyObject*, PyObject* args) {
    PyObject* bits_object = nullptr;
    int axis_bits = 0;
    PyObject* levels_object = nullptr;
    double scale = 1.0;
    PyObject* out_object = nullptr;
    if (!PyArg_ParseTuple(args, "OiOdO", &bits_object, &axis_bits, &levels_object, &scale,
                          &out_object)) {
        return nullptr;
    }
    try {
        if (axis_bits < 1 || axis_bits > 4 || !(scale > 0.0)) {
            throw std::runtime_error("invalid constellation");
        }
        Buffer bits(bits_object, "bits");
        Buffer levels(levels_object, "levels");
        const size_t level_count = static_cast<size_t>(1) << axis_bits;
        if (static_cast<size_t>(levels.bytes()) != level_count * sizeof(float)) {
            throw std::runtime_error("levels must hold 2**axis_bits float32 values");
        }
        const size_t bit_count = static_cast<size_t>(bits.bytes());
        const size_t per_symbol = 2 * static_cast<size_t>(axis_bits);
        if (bit_count % per_symbol != 0) throw std::runtime_error("bit count is not whole symbols");
        const size_t symbols = bit_count / per_symbol;
        Output target(out_object, "out", 2 * symbols * sizeof(float));
        auto* out = static_cast<float*>(target.data());
        bool binary = true;
        {
            AllowThreads unlocked;
            const auto* source = static_cast<const uint8_t*>(bits.data());
            const auto* table = static_cast<const float*>(levels.data());
            float axis[16];
            for (size_t k = 0; k < level_count; ++k) {
                axis[k] = static_cast<float>(static_cast<double>(table[k]) / scale);
            }
            for (size_t s = 0; s < symbols; ++s) {
                const uint8_t* group = source + s * per_symbol;
                unsigned real = 0, imag = 0;
                for (int b = 0; b < axis_bits; ++b) {
                    binary = binary && group[b] <= 1 && group[axis_bits + b] <= 1;
                    real = (real << 1) | (group[b] & 1U);
                    imag = (imag << 1) | (group[axis_bits + b] & 1U);
                }
                out[2 * s] = axis[real];
                out[2 * s + 1] = axis[imag];
            }
        }
        if (!binary) throw std::runtime_error("bits must contain only 0 and 1");
        Py_RETURN_NONE;
    } catch (const std::exception& error) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
}

// modulate(symbols: complex64, first_symbol_index, cp_length, data_bins: int64 x 48,
//          pilot_bins: int64 x 4, pilot_base: float32 x 4, polarity: float32 x P,
//          out: complex64 x ceil(len / 48) * (64 + cp_length))
PyObject* native_modulate(PyObject*, PyObject* args) {
    PyObject* symbols_object = nullptr;
    Py_ssize_t first_symbol_index = 0;
    int cp_length = 0;
    PyObject* data_object = nullptr;
    PyObject* pilot_object = nullptr;
    PyObject* base_object = nullptr;
    PyObject* polarity_object = nullptr;
    PyObject* out_object = nullptr;
    if (!PyArg_ParseTuple(args, "OniOOOOO", &symbols_object, &first_symbol_index, &cp_length,
                          &data_object, &pilot_object, &base_object, &polarity_object,
                          &out_object)) {
        return nullptr;
    }
    try {
        Buffer symbols(symbols_object, "symbols");
        Buffer data(data_object, "data_bins");
        Buffer pilots(pilot_object, "pilot_bins");
        Buffer base(base_object, "pilot_base");
        Buffer polarity(polarity_object, "polarity");
        const size_t data_count = static_cast<size_t>(data.bytes()) / sizeof(int64_t);
        const size_t pilot_count = static_cast<size_t>(pilots.bytes()) / sizeof(int64_t);
        const size_t polarity_count = static_cast<size_t>(polarity.bytes()) / sizeof(float);
        if (data_count == 0 || pilot_count * sizeof(float) != static_cast<size_t>(base.bytes())
            || polarity_count == 0 || cp_length < 1 || cp_length >= kFft
            || first_symbol_index < 0) {
            throw std::runtime_error("invalid modulation tables");
        }
        const auto* data_bins = static_cast<const int64_t*>(data.data());
        const auto* pilot_bins = static_cast<const int64_t*>(pilots.data());
        for (size_t k = 0; k < data_count; ++k) {
            if (data_bins[k] < 0 || data_bins[k] >= kFft) throw std::runtime_error("bad bin");
        }
        for (size_t k = 0; k < pilot_count; ++k) {
            if (pilot_bins[k] < 0 || pilot_bins[k] >= kFft) throw std::runtime_error("bad bin");
        }
        const size_t payload = static_cast<size_t>(symbols.bytes()) / (2 * sizeof(float));
        if (payload == 0) throw std::runtime_error("payload_symbols must not be empty");
        const size_t ofdm_symbols = (payload + data_count - 1) / data_count;
        const size_t block = static_cast<size_t>(kFft + cp_length);
        Output target(out_object, "out", 2 * ofdm_symbols * block * sizeof(float));
        auto* out = static_cast<float*>(target.data());
        {
            AllowThreads unlocked;
            const auto* values = static_cast<const float*>(symbols.data());
            const auto* base_values = static_cast<const float*>(base.data());
            const auto* polarity_values = static_cast<const float*>(polarity.data());
            C grid[kFft], useful[kFft];
            for (size_t s = 0; s < ofdm_symbols; ++s) {
                for (int k = 0; k < kFft; ++k) grid[k] = {0.0, 0.0};
                for (size_t d = 0; d < data_count; ++d) {
                    const size_t index = s * data_count + d;
                    if (index < payload) {
                        grid[data_bins[d]] = {values[2 * index], values[2 * index + 1]};
                    }
                }
                const float sign = polarity_values[(static_cast<size_t>(first_symbol_index) + s)
                                                   % polarity_count];
                for (size_t p = 0; p < pilot_count; ++p) {
                    grid[pilot_bins[p]] = {static_cast<double>(sign * base_values[p]), 0.0};
                }
                ifft64(grid, useful);
                float* row = out + 2 * s * block;
                for (int k = 0; k < cp_length; ++k) {
                    const C& v = useful[kFft - cp_length + k];
                    row[2 * k] = static_cast<float>(v.re);
                    row[2 * k + 1] = static_cast<float>(v.im);
                }
                for (int k = 0; k < kFft; ++k) {
                    row[2 * (cp_length + k)] = static_cast<float>(useful[k].re);
                    row[2 * (cp_length + k) + 1] = static_cast<float>(useful[k].im);
                }
            }
        }
        Py_RETURN_NONE;
    } catch (const std::exception& error) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
}

PyMethodDef methods[] = {
    {"gather", native_gather, METH_VARARGS, "values[index] for a uint8 buffer and int32 index."},
    {"map_bits", native_map_bits, METH_VARARGS, "Map bits to square-QAM complex64 symbols."},
    {"modulate", native_modulate, METH_VARARGS,
     "Insert pilots, IFFT and prepend the cyclic prefix for each OFDM symbol."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_tx_native",
    "Native transmit rate matching, symbol mapping and OFDM modulation.",
    -1,
    methods,
};

}  // namespace

PyMODINIT_FUNC PyInit__tx_native() {
    return PyModule_Create(&module);
}
