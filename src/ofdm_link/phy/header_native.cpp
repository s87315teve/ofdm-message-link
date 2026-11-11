// Native v5 header probe: the receive owner's LTF-pair measurement, delay-
// limited channel smoothing and SIG equalization, ending in the 144 SIG LLRs
// the (already native) Viterbi decodes.  It follows sync_v5.measure_ltf_pair,
// channel.refine_channel_estimate and channel.equalize_with_pilots
// (common_phase_only) step by step, rounding to float32 where those round to
// complex64, and releases the GIL.  The shared channel.py functions the decode
// workers run are not touched: this is an owner-only path, and the Python one
// stays the reference and the fallback (ofdm_link.phy.native_backend).

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr int kFft = 64;
constexpr int kCp = 16;
constexpr int kBlock = kFft + kCp;
constexpr int kActive = 52;
constexpr int kPilots = 4;
constexpr int kData = 48;
constexpr int kSigSymbols = 3;
constexpr int kSigOffset = 320;
constexpr int kFixed = 560;
constexpr double kMinimumNoiseFraction = 1e-6;
constexpr double kMinimumMagnitude = 1e-8;
constexpr double kMinimumPilot = 1e-6;
constexpr float kLlrClip = 64.0F;

struct C {
    double re;
    double im;
};

inline C cadd(C a, C b) { return {a.re + b.re, a.im + b.im}; }
inline C csub(C a, C b) { return {a.re - b.re, a.im - b.im}; }
inline C cmul(C a, C b) { return {a.re * b.re - a.im * b.im, a.re * b.im + a.im * b.re}; }
inline C cscale(C a, double s) { return {a.re * s, a.im * s}; }
inline C cdiv(C a, C b) {
    const double d = b.re * b.re + b.im * b.im;
    return {(a.re * b.re + a.im * b.im) / d, (a.im * b.re - a.re * b.im) / d};
}
inline double cabs(C a) { return std::hypot(a.re, a.im); }
inline C to_single(C a) {
    return {static_cast<double>(static_cast<float>(a.re)),
            static_cast<double>(static_cast<float>(a.im))};
}

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

template <typename T>
std::vector<T> read_array(PyObject* object, const char* name, size_t expected) {
    Buffer buffer(object, name);
    if (static_cast<size_t>(buffer.bytes()) != expected * sizeof(T)) {
        throw std::runtime_error(std::string(name) + " has the wrong length");
    }
    std::vector<T> values(expected);
    std::memcpy(values.data(), buffer.data(), expected * sizeof(T));
    return values;
}

struct Constants {
    std::vector<int64_t> active_bins;  // 52
    std::vector<double> references;    // 52 LTF BPSK values
    std::vector<C> projection;         // 52 x 52, row major
    int residual_dof = 0;
    double residual_limit = 0.0;
    std::vector<int64_t> pilot_indices;  // 4, into the active carriers
    std::vector<int64_t> data_indices;   // 48
    std::vector<C> expected_pilots;      // 3 x 4 (SIG symbols 0..2)
};

// Iterative radix-2 FFT of 64 points, forward, unitary (norm="ortho").
void fft64(const C* input, C* output) {
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
                twiddle[k] = {std::cos(-2.0 * kPi * k / kFft), std::sin(-2.0 * kPi * k / kFft)};
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
    for (int i = 0; i < kFft; ++i) output[i] = cscale(output[i], 0.125);
}

// numpy.remainder(x, 1.0)
inline double remainder_one(double x) {
    double mod = std::fmod(x, 1.0);
    if (mod != 0.0) {
        if (mod < 0.0) mod += 1.0;
    } else {
        mod = std::copysign(0.0, 1.0);
    }
    return mod;
}

// sync_v5.correct_v5_cfo, rounded to complex64 as it returns.
void correct_cfo(const float* v, long first, int count, long stf_start, double cfo, C* out) {
    const long offset = first - stf_start;
    const double per_sample = cfo / 64.0;
    for (int k = 0; k < count; ++k) {
        double phase = static_cast<double>(offset + k);
        phase *= per_sample;
        phase = remainder_one(phase);
        phase *= -2.0 * kPi;
        const C sample{static_cast<double>(v[2 * (first + k)]),
                       static_cast<double>(v[2 * (first + k) + 1])};
        out[k] = to_single(cmul(sample, {std::cos(phase), std::sin(phase)}));
    }
}

enum Status { kOk = 0, kLtfUnusable = 1, kChannelError = 2 };

struct Probe {
    int status = kOk;
    std::string message;
    std::vector<float> llrs;
};

Probe probe(const float* v, long n, long stf_start, long ltf_start, double cfo, int backoff,
            bool average, bool smoothing, const Constants& k) {
    Probe result;
    // measure_ltf_pair
    const long start = ltf_start - backoff;
    if (start < 0 || start + 128 > n) {
        result.status = kLtfUnusable;
        return result;
    }
    C corrected[128];
    correct_cfo(v, start, 128, stf_start, cfo, corrected);
    C spectrum[2][kFft];
    fft64(corrected, spectrum[0]);
    fft64(corrected + kFft, spectrum[1]);
    C first[kActive], second[kActive];
    double noise = 0.0, signal = 0.0;
    for (int a = 0; a < kActive; ++a) {
        first[a] = spectrum[0][k.active_bins[static_cast<size_t>(a)]];
        second[a] = spectrum[1][k.active_bins[static_cast<size_t>(a)]];
        const double d = cabs(csub(first[a], second[a]));
        const double s = cabs(cadd(first[a], second[a]));
        noise += d * d;
        signal += s * s;
    }
    noise = noise / kActive / 2.0;
    signal = signal / kActive / 4.0 - noise / 2.0;
    if (!(signal > 0.0)) {
        result.status = kLtfUnusable;
        return result;
    }
    C least_squares[kActive];
    for (int a = 0; a < kActive; ++a) {
        const double reference = k.references[static_cast<size_t>(a)];
        const C value = average ? cscale(cadd(first[a], second[a]), 1.0 / (2.0 * reference))
                                : cscale(first[a], 1.0 / reference);
        least_squares[a] = to_single(value);
        const float magnitude = std::hypot(static_cast<float>(least_squares[a].re),
                                           static_cast<float>(least_squares[a].im));
        if (!std::isfinite(least_squares[a].re) || !std::isfinite(least_squares[a].im)
            || magnitude < kMinimumMagnitude) {
            result.status = kLtfUnusable;
            return result;
        }
    }
    const double ltf_noise = std::max(noise, signal * kMinimumNoiseFraction);

    // refine_channel_estimate, or the raw estimate without smoothing
    C channel[kActive];
    double noise_variance = ltf_noise;
    std::copy(least_squares, least_squares + kActive, channel);
    if (smoothing) {
        if (!std::isfinite(ltf_noise) || ltf_noise <= 0.0) {
            result.status = kChannelError;
            result.message = "preamble_noise_variance must be a positive finite number";
            return result;
        }
        C smoothed[kActive];
        double residual = 0.0;
        for (int row = 0; row < kActive; ++row) {
            C sum{0.0, 0.0};
            for (int column = 0; column < kActive; ++column) {
                sum = cadd(sum, cmul(k.projection[static_cast<size_t>(row * kActive + column)],
                                     least_squares[column]));
            }
            smoothed[row] = sum;
            const C rest = csub(least_squares[row], sum);
            residual += rest.re * rest.re + rest.im * rest.im;
        }
        double residual_variance = residual / k.residual_dof;
        if (average) residual_variance *= 2;
        if (!(residual_variance > k.residual_limit * ltf_noise)) {
            const double dof = kActive;
            noise_variance = (dof * ltf_noise + k.residual_dof * residual_variance)
                / (dof + k.residual_dof);
            for (int a = 0; a < kActive; ++a) {
                channel[a] = to_single(smoothed[a]);
                const float magnitude = std::hypot(static_cast<float>(channel[a].re),
                                                   static_cast<float>(channel[a].im));
                if (magnitude < kMinimumMagnitude) {
                    result.status = kChannelError;
                    result.message = "coefficients contains a zero or near-zero value";
                    return result;
                }
            }
        }
    }

    // SIG: CFO correction, FFT, common-phase-only pilot equalization
    const long sig_first = stf_start + kSigOffset - backoff;
    C sig[kSigSymbols * kBlock];
    correct_cfo(v, sig_first, kSigSymbols * kBlock, stf_start, cfo, sig);
    float pilot_power[kPilots];
    for (int p = 0; p < kPilots; ++p) {
        const C h = channel[k.pilot_indices[static_cast<size_t>(p)]];
        const float magnitude = std::hypot(static_cast<float>(h.re), static_cast<float>(h.im));
        pilot_power[p] = magnitude * magnitude;
    }
    float inverse_power[kData];
    for (int d = 0; d < kData; ++d) {
        const C h = channel[k.data_indices[static_cast<size_t>(d)]];
        const float magnitude = std::hypot(static_cast<float>(h.re), static_cast<float>(h.im));
        inverse_power[d] = 1.0F / (magnitude * magnitude);
    }
    const float noise32 = static_cast<float>(noise_variance);
    result.llrs.resize(kSigSymbols * kData);
    for (int symbol = 0; symbol < kSigSymbols; ++symbol) {
        C grid[kFft];
        fft64(sig + symbol * kBlock + kCp, grid);
        C equalized[kActive];
        for (int a = 0; a < kActive; ++a) {
            equalized[a] = to_single(cdiv(to_single(grid[k.active_bins[static_cast<size_t>(a)]]),
                                          channel[a]));
        }
        C weighted{0.0, 0.0};
        for (int p = 0; p < kPilots; ++p) {
            const C received = equalized[k.pilot_indices[static_cast<size_t>(p)]];
            if (cabs(received) < kMinimumPilot) {
                result.status = kChannelError;
                result.message = "received pilot contains a zero or low-power value";
                result.llrs.clear();
                return result;
            }
            const C residual = to_single(
                cdiv(received, k.expected_pilots[static_cast<size_t>(symbol * kPilots + p)]));
            weighted = cadd(weighted, cscale(residual, pilot_power[p]));
        }
        const float common_phase = static_cast<float>(
            std::atan2(static_cast<float>(weighted.im), static_cast<float>(weighted.re)));
        const C rotation{static_cast<double>(std::cos(common_phase)),
                         -static_cast<double>(std::sin(common_phase))};
        for (int d = 0; d < kData; ++d) {
            const C value = to_single(
                cmul(equalized[k.data_indices[static_cast<size_t>(d)]], rotation));
            const float numerator = 4.0F * static_cast<float>(value.re);
            const float denominator = inverse_power[d] * noise32;
            const float llr = numerator / denominator;
            result.llrs[static_cast<size_t>(symbol * kData + d)] =
                std::min(kLlrClip, std::max(-kLlrClip, llr));
        }
    }
    return result;
}

Constants read_constants(PyObject* object) {
    if (!PyTuple_Check(object) || PyTuple_GET_SIZE(object) != 8) {
        throw std::runtime_error("constants must be an 8-tuple");
    }
    Constants k;
    k.active_bins = read_array<int64_t>(PyTuple_GET_ITEM(object, 0), "active_bins", kActive);
    k.references = read_array<double>(PyTuple_GET_ITEM(object, 1), "references", kActive);
    k.projection = read_array<C>(PyTuple_GET_ITEM(object, 2), "projection", kActive * kActive);
    k.residual_dof = static_cast<int>(PyLong_AsLong(PyTuple_GET_ITEM(object, 3)));
    k.residual_limit = PyFloat_AsDouble(PyTuple_GET_ITEM(object, 4));
    if (PyErr_Occurred() || k.residual_dof <= 0) {
        throw std::runtime_error("invalid smoothing constants");
    }
    k.pilot_indices = read_array<int64_t>(PyTuple_GET_ITEM(object, 5), "pilot_indices", kPilots);
    k.data_indices = read_array<int64_t>(PyTuple_GET_ITEM(object, 6), "data_indices", kData);
    k.expected_pilots =
        read_array<C>(PyTuple_GET_ITEM(object, 7), "expected_pilots", kSigSymbols * kPilots);
    for (auto bin : k.active_bins) {
        if (bin < 0 || bin >= kFft) throw std::runtime_error("active bin out of range");
    }
    for (auto index : k.pilot_indices) {
        if (index < 0 || index >= kActive) throw std::runtime_error("pilot index out of range");
    }
    for (auto index : k.data_indices) {
        if (index < 0 || index >= kActive) throw std::runtime_error("data index out of range");
    }
    return k;
}

// sig_llrs(samples, stf_start, ltf_start, cfo, backoff, average, smoothing, constants)
//   -> (0, llr bytes) | (1, None) for an unusable LTF pair | (2, message)
PyObject* native_sig_llrs(PyObject*, PyObject* args) {
    PyObject* samples_object = nullptr;
    Py_ssize_t stf_start = 0, ltf_start = 0;
    double cfo = 0.0;
    int backoff = 0, average = 1, smoothing = 1;
    PyObject* constants_object = nullptr;
    if (!PyArg_ParseTuple(args, "OnndippO", &samples_object, &stf_start, &ltf_start, &cfo,
                          &backoff, &average, &smoothing, &constants_object)) {
        return nullptr;
    }
    try {
        const Constants constants = read_constants(constants_object);
        Buffer buffer(samples_object, "samples");
        if (buffer.bytes() % (2 * static_cast<Py_ssize_t>(sizeof(float))) != 0) {
            throw std::runtime_error("samples must be complex64");
        }
        const long n = static_cast<long>(buffer.bytes() / (2 * sizeof(float)));
        if (stf_start < 0 || stf_start + kFixed > n || backoff < 0 || backoff > 8
            || stf_start + kSigOffset - backoff < 0) {
            throw std::runtime_error("the fixed part of the burst is not in the samples");
        }
        Probe result;
        {
            AllowThreads unlocked;
            result = probe(static_cast<const float*>(buffer.data()), n, stf_start, ltf_start, cfo,
                           backoff, average != 0, smoothing != 0, constants);
        }
        if (result.status == kOk) {
            PyObject* llrs = PyBytes_FromStringAndSize(
                reinterpret_cast<const char*>(result.llrs.data()),
                static_cast<Py_ssize_t>(result.llrs.size() * sizeof(float)));
            if (llrs == nullptr) return nullptr;
            PyObject* value = Py_BuildValue("(iN)", kOk, llrs);
            return value;
        }
        if (result.status == kLtfUnusable) return Py_BuildValue("(iO)", kLtfUnusable, Py_None);
        return Py_BuildValue("(is)", kChannelError, result.message.c_str());
    } catch (const std::exception& error) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
}

PyMethodDef methods[] = {
    {"sig_llrs", native_sig_llrs, METH_VARARGS,
     "SIG LLRs of an acquired v5 burst: LTF pair, smoothing, SIG equalization."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_header_native",
    "Native v5 header probe for the receive owner.",
    -1,
    methods,
};

}  // namespace

PyMODINIT_FUNC PyInit__header_native() {
    return PyModule_Create(&module);
}
