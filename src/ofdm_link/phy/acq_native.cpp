// Native preamble acquisition: the lag-32 shared correlation, the legacy
// repeated-half detector and the wire-v5 STF/LTF detector of sync.py and
// sync_v5.py in one pass that releases the GIL.
//
// Every stage follows the Python reference step by step (same formulas, same
// evaluation order, float32 products and float64 accumulation where NumPy
// uses them), so the candidates match it to rounding: identical start
// positions, CFO within 1e-6 and confidences within 1e-5 relative.  The
// Python implementation stays the reference and the fallback
// (ofdm_link.phy.native_backend).  Plain C++17, no ISA-specific code.

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <numeric>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr int kSteadySide = 448;
constexpr int kSteadyMinimum = 128;
constexpr int kSteadyStep = 16;
constexpr double kSteadyK2 = 16.0;  // _STEADY_SIGNIFICANCE ** 2
constexpr double kSteadySignificance = 4.0;
constexpr int kPhasorBlock = 8;

// v5 layout (sync_v5.py)
constexpr int kMetricWidth = 128;
constexpr int kStfPeriod = 16;
constexpr int kStfSkip = 16;
constexpr int kStfCheck = 128;
constexpr int kCoarseLag = 16;
constexpr int kLtfFirst = 192;
constexpr int kPreambleV5 = 320;
constexpr int kFixedV5 = 560;
constexpr int kPeakRadius = 160;
constexpr int kLocalSpan = 1024;

struct C {
    double re;
    double im;
};

inline C cmul(C a, C b) { return {a.re * b.re - a.im * b.im, a.re * b.im + a.im * b.re}; }
inline C cconjmul(C a, C b) {  // conj(a) * b
    return {a.re * b.re + a.im * b.im, a.re * b.im - a.im * b.re};
}
inline C cadd(C a, C b) { return {a.re + b.re, a.im + b.im}; }
inline C csub(C a, C b) { return {a.re - b.re, a.im - b.im}; }
inline C cscale(C a, double s) { return {a.re * s, a.im * s}; }
inline double cabs(C a) { return std::hypot(a.re, a.im); }
inline double cabs2(C a) {
    const double m = std::hypot(a.re, a.im);
    return m * m;
}
inline C cexpi(double theta) { return {std::cos(theta), std::sin(theta)}; }
inline double cangle(C a) { return std::atan2(a.im, a.re); }

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

std::vector<C> read_complex128(PyObject* object, const char* name, size_t expected) {
    Buffer buffer(object, name);
    if (static_cast<size_t>(buffer.bytes()) != expected * 2 * sizeof(double)) {
        throw std::runtime_error(std::string(name) + " has the wrong length");
    }
    std::vector<C> values(expected);
    std::memcpy(values.data(), buffer.data(), expected * sizeof(C));
    return values;
}

// ---------------------------------------------------------------------------
// shared lag-L correlation (sync._shared_correlation)

struct Shared {
    std::vector<double> rc, ic, ec;  // cumulative sums, one entry per product
    long products = 0;
};

Shared shared_correlation(const float* v, long n, int lag) {
    Shared shared;
    const long count = n - lag;
    shared.products = count;
    shared.rc.resize(static_cast<size_t>(count));
    shared.ic.resize(static_cast<size_t>(count));
    shared.ec.resize(static_cast<size_t>(count));
    double rc = 0.0, ic = 0.0, ec = 0.0;
    for (long k = 0; k < count; ++k) {
        const float xr = v[2 * k], xi = v[2 * k + 1];
        const float yr = v[2 * (k + lag)], yi = v[2 * (k + lag) + 1];
        const float pr = xr * yr + xi * yi;
        const float pi = xr * yi - xi * yr;
        float sx = xr * xr;
        sx += xi * xi;
        float sy = yr * yr;
        sy += yi * yi;
        float paired = sx + sy;
        paired *= 0.5F;
        rc += static_cast<double>(pr);
        ic += static_cast<double>(pi);
        ec += static_cast<double>(paired);
        shared.rc[static_cast<size_t>(k)] = rc;
        shared.ic[static_cast<size_t>(k)] = ic;
        shared.ec[static_cast<size_t>(k)] = ec;
    }
    return shared;
}

// Entry d sums products d .. d + width - 1 (SharedCorrelation.window).
inline double window_sum(const std::vector<double>& cum, long d, int width) {
    if (d == 0) return cum[static_cast<size_t>(width - 1)];
    return cum[static_cast<size_t>(d + width - 1)] - cum[static_cast<size_t>(d - 1)];
}

// z = [0 x (side + 1), cum, cum[-1] x (side + width)] read at index j.
inline double padded(const std::vector<double>& cum, long j) {
    if (j <= kSteadySide) return 0.0;
    const long index = j - kSteadySide - 1;
    if (index < static_cast<long>(cum.size())) return cum[static_cast<size_t>(index)];
    return cum.back();
}

struct SteadyGrid {
    std::vector<double> real, imag;
    std::vector<uint8_t> significant;
    bool any_significant = false;
};

// sync._steady_grid
SteadyGrid steady_grid(const Shared& shared, long limit, int width) {
    SteadyGrid grid;
    const long points = (limit + kSteadyStep - 1) / kSteadyStep;
    grid.real.resize(static_cast<size_t>(points));
    grid.imag.resize(static_cast<size_t>(points));
    grid.significant.resize(static_cast<size_t>(points));
    const long products = shared.products;
    const long start = kSteadySide + width;
    for (long j = 0; j < points; ++j) {
        const long g = j * kSteadyStep;
        const long before_count = std::min<long>(g, kSteadySide);
        const long after_count = std::min<long>(products - width - g, kSteadySide);
        const bool trusted_before = before_count >= kSteadyMinimum;
        const bool trusted_after = after_count >= kSteadyMinimum;
        const double before_scale =
            trusted_before ? 1.0 / static_cast<double>(std::max<long>(before_count, 1)) : 0.0;
        const double after_scale =
            trusted_after ? 1.0 / static_cast<double>(std::max<long>(after_count, 1)) : 0.0;
        double before[3], after[3];
        const std::vector<double>* cums[3] = {&shared.rc, &shared.ic, &shared.ec};
        for (int c = 0; c < 3; ++c) {
            const auto& cum = *cums[c];
            const double b = padded(cum, kSteadySide + g) - padded(cum, g);
            const double a = padded(cum, start + kSteadySide + g) - padded(cum, start + g);
            before[c] = b * before_scale;
            after[c] = a * after_scale;
        }
        const double before_steady = before[0] * before[0] + before[1] * before[1];
        const double after_steady = after[0] * after[0] + after[1] * after[1];
        const double inf = std::numeric_limits<double>::infinity();
        const bool use_before =
            (trusted_before ? before_steady : inf) <= (trusted_after ? after_steady : inf);
        grid.real[static_cast<size_t>(j)] = use_before ? before[0] : after[0];
        grid.imag[static_cast<size_t>(j)] = use_before ? before[1] : after[1];
        const bool loud_before =
            before_steady * static_cast<double>(std::max<long>(before_count, 1))
            > kSteadyK2 * (before[2] * before[2]);
        const bool loud_after =
            after_steady * static_cast<double>(std::max<long>(after_count, 1))
            > kSteadyK2 * (after[2] * after[2]);
        const bool significant = (loud_before || !trusted_before)
            && (loud_after || !trusted_after) && (trusted_before || trusted_after);
        grid.significant[static_cast<size_t>(j)] = significant ? 1 : 0;
        grid.any_significant = grid.any_significant || significant;
    }
    return grid;
}

// ---------------------------------------------------------------------------
// legacy repeated-half detector (sync._acquisition_candidates)

struct LegacyParams {
    double detection_threshold;
    double correlation_threshold;
    double minimum_power;
    std::vector<C> reference;  // conjugated CP-prefixed preamble, 80 values
    double reference_energy;
};

struct LegacyCandidate {
    long start;
    long useful_start;
    double cfo;
    double amplitude;
    double metric;
    double confidence;
};

constexpr int kHalf = 32;
constexpr int kCp = 16;
constexpr int kFft = 64;
constexpr int kLegacyPreamble = kCp + kFft;

std::vector<LegacyCandidate> legacy_candidates(
    const float* v, long n, const Shared& shared, const LegacyParams& p
) {
    std::vector<LegacyCandidate> found;
    if (n < kLegacyPreamble) return found;
    const long first_useful = kCp;
    const long last_useful = n - kFft;
    const long limit = last_useful + 1;

    // _significant_steady(shared, half, limit)
    bool steady = false;
    SteadyGrid grid;
    {
        const double total = shared.rc.back() * shared.rc.back()
            + shared.ic.back() * shared.ic.back();
        const double energy = shared.ec.back();
        if (limit > 0
            && total * static_cast<double>(shared.products) > kSteadyK2 * (energy * energy)) {
            grid = steady_grid(shared, limit, kHalf);
            steady = grid.any_significant;
        }
    }
    std::vector<double> steady_magnitude;
    if (steady) {
        steady_magnitude.resize(grid.real.size());
        for (size_t j = 0; j < grid.real.size(); ++j) {
            if (!grid.significant[j]) {
                grid.real[j] = 0.0;
                grid.imag[j] = 0.0;
            }
            steady_magnitude[j] = std::hypot(grid.real[j], grid.imag[j]);
        }
    }

    const double powered_floor = p.minimum_power * kHalf;
    const double window_floor = p.minimum_power * kLegacyPreamble;
    for (long d = first_useful; d <= last_useful; ++d) {
        const double re = window_sum(shared.rc, d, kHalf);
        const double im = window_sum(shared.ic, d, kHalf);
        const double en = window_sum(shared.ec, d, kHalf);
        const bool powered = en >= powered_floor;
        double numerator = re * re;
        numerator += im * im;
        bool eligible;
        if (!steady) {
            double threshold = en * en;
            threshold *= p.detection_threshold;
            eligible = numerator >= threshold;
        } else {
            const size_t g = static_cast<size_t>(d / kSteadyStep);
            const double gate_real = re - kHalf * grid.real[g];
            const double gate_imag = im - kHalf * grid.imag[g];
            const double gate_energy = en - kHalf * steady_magnitude[g];
            double gate_numerator = gate_real * gate_real;
            gate_numerator += gate_imag * gate_imag;
            double threshold =
                gate_energy * (gate_energy + (2.0 * kHalf) * steady_magnitude[g]);
            threshold *= p.detection_threshold;
            eligible = gate_numerator >= threshold && gate_energy > 0.0;
        }
        if (!(eligible && powered)) continue;

        const long start = d - kCp;
        const double metric = numerator / (en * en);
        const double cfo = std::atan2(im, re) / kPi;
        const double step = -2.0 * kPi * cfo / kFft;
        C fine[kPhasorBlock];
        for (int r = 0; r < kPhasorBlock; ++r) fine[r] = cexpi(step * r);
        C coherent{0.0, 0.0};
        double window_energy = 0.0;
        const float* window = v + 2 * start;
        for (int q = 0; q < kLegacyPreamble / kPhasorBlock; ++q) {
            C partial{0.0, 0.0};
            for (int r = 0; r < kPhasorBlock; ++r) {
                const int k = q * kPhasorBlock + r;
                const C sample{static_cast<double>(window[2 * k]),
                               static_cast<double>(window[2 * k + 1])};
                partial = cadd(partial, cmul(cmul(sample, p.reference[static_cast<size_t>(k)]),
                                             fine[r]));
            }
            coherent = cadd(coherent, cmul(partial, cexpi(step * (q * kPhasorBlock))));
        }
        for (int k = 0; k < 2 * kLegacyPreamble; ++k) {
            const double value = static_cast<double>(window[k]);
            window_energy += value * value;
        }
        if (!(window_energy >= window_floor)) continue;
        const double confidence = cabs2(coherent) / (window_energy * p.reference_energy);
        if (!(confidence >= p.correlation_threshold)) continue;
        const C gain{coherent.re / p.reference_energy, coherent.im / p.reference_energy};
        found.push_back({start, d, cfo, cabs(gain), metric, confidence});
    }

    // sync.separated_candidates: strongest first, at least a preamble apart.
    std::vector<size_t> order(found.size());
    std::iota(order.begin(), order.end(), 0);
    std::stable_sort(order.begin(), order.end(), [&](size_t a, size_t b) {
        return found[a].confidence > found[b].confidence;
    });
    std::vector<LegacyCandidate> selected;
    for (size_t index : order) {
        const auto& candidate = found[index];
        bool apart = true;
        for (const auto& other : selected) {
            if (std::labs(candidate.start - other.start) < kLegacyPreamble) {
                apart = false;
                break;
            }
        }
        if (apart) selected.push_back(candidate);
    }
    std::stable_sort(selected.begin(), selected.end(),
                     [](const LegacyCandidate& a, const LegacyCandidate& b) {
                         return a.start < b.start;
                     });
    return selected;
}

// ---------------------------------------------------------------------------
// wire-v5 detector (sync_v5._v5_candidates)

struct V5Params {
    double detection_threshold;
    double stf_threshold;
    double correlation_threshold;
    double balance_ratio;
    int fine_timing_search;
    double minimum_power;
    std::vector<C> stf_phases;  // 16 x 16, row = cyclic phase
    double stf_reference_energy;
    std::vector<C> ltf_reference;  // conj(LTF symbol), 64 values
    double ltf_reference_power;
};

struct V5Candidate {
    long stf_start;
    long ltf_start;
    double cfo;
    double coarse;
    double metric;
    double stf_confidence;
    double confidence;
};

// sync_v5._LocalInterference, for one candidate.
struct Local {
    std::vector<C> samples;  // DC removed
    C dc{0.0, 0.0};
    long preamble = 0;
    double power = 0.0;

    Local(const float* v, long n, long start) {
        const long span = std::min<long>(kLocalSpan, n);
        long first = start - (span - kPreambleV5) / 2;
        first = std::max<long>(0, std::min<long>(first, n - span));
        samples.resize(static_cast<size_t>(span));
        C total{0.0, 0.0};
        for (long j = 0; j < span; ++j) {
            const C value{static_cast<double>(v[2 * (first + j)]),
                          static_cast<double>(v[2 * (first + j) + 1])};
            samples[static_cast<size_t>(j)] = value;
            total = cadd(total, value);
        }
        dc = cscale(total, 1.0 / static_cast<double>(span));
        double power_sum = 0.0;
        for (auto& value : samples) {
            value = csub(value, dc);
            power_sum += value.re * value.re + value.im * value.im;
        }
        power = power_sum / static_cast<double>(span);
        preamble = start - first;
    }

    C steady(int lag) const {
        const long span = static_cast<long>(samples.size());
        C sum{0.0, 0.0};
        long count = 0;
        for (long j = 0; j + lag < span; ++j) {
            const long after = j - (preamble + kPreambleV5);
            bool outside = (j < preamble - lag) || (after >= 0);
            if (lag == 64) outside = outside && ((after < 0) || (after % 80 >= 16));
            if (!outside) continue;
            sum = cadd(sum, cconjmul(samples[static_cast<size_t>(j)],
                                     samples[static_cast<size_t>(j + lag)]));
            ++count;
        }
        count = std::max<long>(count, 1);
        const C mean = cscale(sum, 1.0 / static_cast<double>(count));
        const bool significant =
            cabs(mean) > kSteadySignificance * power / std::sqrt(static_cast<double>(count));
        return significant ? mean : C{0.0, 0.0};
    }
};

inline double nan_to_num(double value) {
    if (std::isnan(value)) return 0.0;
    if (std::isinf(value)) {
        return value > 0 ? std::numeric_limits<double>::max()
                         : -std::numeric_limits<double>::max();
    }
    return value;
}

std::vector<V5Candidate> v5_candidates(
    const float* v, long n, const Shared& shared, const V5Params& p
) {
    std::vector<V5Candidate> found;
    const int search = p.fine_timing_search;
    const long limit = n - (kLtfFirst + search + 128);
    if (limit <= 0 || shared.products < kMetricWidth) return found;

    SteadyGrid grid = steady_grid(shared, limit, kMetricWidth);
    std::vector<double> steady_magnitude(grid.real.size());
    for (size_t j = 0; j < grid.real.size(); ++j) {
        steady_magnitude[j] = std::hypot(grid.real[j], grid.imag[j]);
    }
    const double raw_floor = p.minimum_power * kMetricWidth;

    std::vector<long> offsets;
    std::vector<double> numerators;
    std::vector<double> energies;
    for (long d = 0; d < limit; ++d) {
        const size_t g = static_cast<size_t>(d / kSteadyStep);
        const double re = window_sum(shared.rc, d, kMetricWidth) - kMetricWidth * grid.real[g];
        const double im = window_sum(shared.ic, d, kMetricWidth) - kMetricWidth * grid.imag[g];
        const double raw = window_sum(shared.ec, d, kMetricWidth);
        double numerator = re * re;
        numerator += im * im;
        const double energy = raw - kMetricWidth * steady_magnitude[g];
        double threshold = energy * energy;
        if (grid.any_significant) {
            const double cross =
                grid.significant[g] ? 2.0 * kMetricWidth * steady_magnitude[g] : 0.0;
            threshold = energy * (energy + cross);
        }
        threshold *= p.detection_threshold;
        if (numerator >= threshold && energy > 0.0 && raw >= raw_floor) {
            offsets.push_back(d);
            numerators.push_back(numerator);
            energies.push_back(energy);
        }
    }
    if (offsets.empty()) return found;

    // Non-maximum suppression: the strongest remaining offset suppresses its
    // +-160 neighbourhood; ties go to the first offset (argmax order).
    std::vector<size_t> order(offsets.size());
    std::iota(order.begin(), order.end(), 0);
    std::stable_sort(order.begin(), order.end(),
                     [&](size_t a, size_t b) { return numerators[a] > numerators[b]; });
    std::vector<uint8_t> suppressed(offsets.size(), 0);
    std::vector<size_t> peaks;
    for (size_t index : order) {
        if (suppressed[index]) continue;
        const long peak = offsets[index];
        const auto first = std::lower_bound(offsets.begin(), offsets.end(), peak - kPeakRadius);
        const auto last = std::upper_bound(offsets.begin(), offsets.end(), peak + kPeakRadius);
        for (auto it = first; it != last; ++it) {
            suppressed[static_cast<size_t>(it - offsets.begin())] = 1;
        }
        if (peak + kPeakRadius < limit) peaks.push_back(index);
    }
    std::sort(peaks.begin(), peaks.end(),
              [&](size_t a, size_t b) { return offsets[a] < offsets[b]; });

    const double stf_floor = p.minimum_power * kStfCheck;
    const double pair_floor = p.minimum_power * 128;
    const int positions = 2 * search + 1;
    const int block_length = 2 * search + 128;
    std::vector<C> corrected(kStfCheck), block(static_cast<size_t>(block_length));
    std::vector<C> symbol_corr(static_cast<size_t>(block_length - 64 + 1));
    std::vector<double> cumulative(static_cast<size_t>(block_length + 1));
    std::vector<double> pair(static_cast<size_t>(positions));

    for (size_t peak : peaks) {
        const long s = offsets[peak];
        const double energy = std::max(energies[peak], 1e-300);
        const double metric = numerators[peak] / (energy * energy);
        const Local local(v, n, s);
        const C dc = local.dc;

        // Coarse CFO from the lag-16 STF correlation.
        C lag16{0.0, 0.0};
        for (int k = 0; k < kStfCheck; ++k) {
            const long a = s + kStfSkip + k;
            const C first{v[2 * a] - dc.re, v[2 * a + 1] - dc.im};
            const C lagged{v[2 * (a + kCoarseLag)] - dc.re, v[2 * (a + kCoarseLag) + 1] - dc.im};
            lag16 = cadd(lag16, cconjmul(first, lagged));
            corrected[static_cast<size_t>(k)] = first;
        }
        lag16 = csub(lag16, cscale(local.steady(kCoarseLag), kStfCheck));
        const double coarse = 4.0 * cangle(lag16) / (2.0 * kPi);

        // Coherent STF check over the 16 cyclic phases.
        double window_energy = 0.0;
        C folded[kStfPeriod] = {};
        for (int k = 0; k < kStfCheck; ++k) {
            const double theta = -2.0 * kPi * coarse * (kStfSkip + k) / 64.0;
            const C value = cmul(corrected[static_cast<size_t>(k)], cexpi(theta));
            folded[k % kStfPeriod] = cadd(folded[k % kStfPeriod], value);
            window_energy += cabs2(value);
        }
        double stf_confidence = 0.0;
        if (window_energy >= stf_floor) {
            double best = 0.0;
            for (int phase = 0; phase < kStfPeriod; ++phase) {
                C coherent{0.0, 0.0};
                for (int k = 0; k < kStfPeriod; ++k) {
                    coherent = cadd(coherent, cmul(folded[k],
                                                   p.stf_phases[static_cast<size_t>(phase * kStfPeriod + k)]));
                }
                best = std::max(best, cabs2(coherent));
            }
            stf_confidence = best / (window_energy * p.stf_reference_energy);
        }
        if (!(stf_confidence >= p.stf_threshold)) continue;

        // Fine timing on the LTF pair, coarse CFO removed.
        for (int j = 0; j < block_length; ++j) {
            const long offset = kLtfFirst - search + j;
            const long a = s + offset;
            const C value{v[2 * a] - dc.re, v[2 * a + 1] - dc.im};
            block[static_cast<size_t>(j)] =
                cmul(value, cexpi(-2.0 * kPi * coarse * static_cast<double>(offset) / 64.0));
        }
        for (size_t position = 0; position < symbol_corr.size(); ++position) {
            C sum{0.0, 0.0};
            for (int k = 0; k < 64; ++k) {
                sum = cadd(sum, cmul(block[position + static_cast<size_t>(k)],
                                     p.ltf_reference[static_cast<size_t>(k)]));
            }
            symbol_corr[position] = sum;
        }
        cumulative[0] = 0.0;
        for (int j = 0; j < block_length; ++j) {
            cumulative[static_cast<size_t>(j + 1)] =
                cumulative[static_cast<size_t>(j)] + cabs2(block[static_cast<size_t>(j)]);
        }
        int best = 0;
        for (int position = 0; position < positions; ++position) {
            const double first_energy = cumulative[static_cast<size_t>(position + 64)]
                - cumulative[static_cast<size_t>(position)];
            const double second_energy = cumulative[static_cast<size_t>(position + 128)]
                - cumulative[static_cast<size_t>(position + 64)];
            const double total = first_energy + second_energy;
            double value = cabs2(cadd(symbol_corr[static_cast<size_t>(position)],
                                      symbol_corr[static_cast<size_t>(position + 64)]))
                / (2.0 * total * p.ltf_reference_power);
            if (!(total >= pair_floor)) value = 0.0;
            pair[static_cast<size_t>(position)] = value;
            if (value > pair[static_cast<size_t>(best)]) best = position;
        }
        const double confidence = pair[static_cast<size_t>(best)];
        const double first_energy = cumulative[static_cast<size_t>(best + 64)]
            - cumulative[static_cast<size_t>(best)];
        const double second_energy = cumulative[static_cast<size_t>(best + 128)]
            - cumulative[static_cast<size_t>(best + 64)];
        const double first_single = nan_to_num(
            cabs2(symbol_corr[static_cast<size_t>(best)]) / (first_energy * p.ltf_reference_power));
        const double second_single = nan_to_num(
            cabs2(symbol_corr[static_cast<size_t>(best + 64)])
            / (second_energy * p.ltf_reference_power));
        const bool balanced =
            std::min(first_single, second_single) >= p.balance_ratio * confidence;
        const long ltf_start = s + kLtfFirst - search + best;
        const long stf_start = ltf_start - kLtfFirst;

        // Fine CFO from the L1 -> L2 rotation, less the steady lag-64 product.
        C rotation{0.0, 0.0};
        for (int k = 0; k < 64; ++k) {
            rotation = cadd(rotation, cconjmul(block[static_cast<size_t>(best + k)],
                                               block[static_cast<size_t>(best + 64 + k)]));
        }
        rotation = csub(rotation, cmul(cscale(local.steady(64), 64.0),
                                       cexpi(-2.0 * kPi * coarse)));
        const double fine = cangle(rotation) / (2.0 * kPi);
        if (confidence >= p.correlation_threshold && balanced && stf_start >= 0) {
            found.push_back({stf_start, ltf_start, coarse + fine, coarse, metric,
                             stf_confidence, confidence});
        }
    }

    // separated_v5_candidates: strongest first, one per 560-sample fixed part.
    std::vector<size_t> rank(found.size());
    std::iota(rank.begin(), rank.end(), 0);
    std::stable_sort(rank.begin(), rank.end(), [&](size_t a, size_t b) {
        return found[a].confidence > found[b].confidence;
    });
    std::vector<V5Candidate> selected;
    for (size_t index : rank) {
        const auto& candidate = found[index];
        bool apart = true;
        for (const auto& other : selected) {
            if (std::labs(candidate.stf_start - other.stf_start) < kFixedV5) {
                apart = false;
                break;
            }
        }
        if (apart) selected.push_back(candidate);
    }
    std::stable_sort(selected.begin(), selected.end(),
                     [](const V5Candidate& a, const V5Candidate& b) {
                         return a.stf_start < b.stf_start;
                     });
    return selected;
}

// ---------------------------------------------------------------------------
// Python glue

double read_double(PyObject* tuple, Py_ssize_t index) {
    const double value = PyFloat_AsDouble(PyTuple_GET_ITEM(tuple, index));
    if (PyErr_Occurred()) throw std::runtime_error("parameter must be a number");
    return value;
}

LegacyParams read_legacy(PyObject* object) {
    if (!PyTuple_Check(object) || PyTuple_GET_SIZE(object) != 5) {
        throw std::runtime_error("legacy parameters must be a 5-tuple");
    }
    LegacyParams p;
    p.detection_threshold = read_double(object, 0);
    p.correlation_threshold = read_double(object, 1);
    p.minimum_power = read_double(object, 2);
    p.reference = read_complex128(PyTuple_GET_ITEM(object, 3), "legacy reference",
                                  kLegacyPreamble);
    p.reference_energy = read_double(object, 4);
    return p;
}

V5Params read_v5(PyObject* object) {
    if (!PyTuple_Check(object) || PyTuple_GET_SIZE(object) != 10) {
        throw std::runtime_error("v5 parameters must be a 10-tuple");
    }
    V5Params p;
    p.detection_threshold = read_double(object, 0);
    p.stf_threshold = read_double(object, 1);
    p.correlation_threshold = read_double(object, 2);
    p.balance_ratio = read_double(object, 3);
    const long search = PyLong_AsLong(PyTuple_GET_ITEM(object, 4));
    if (PyErr_Occurred() || search < 0 || search >= 64) {
        throw std::runtime_error("fine_timing_search must be in [0, 64)");
    }
    p.fine_timing_search = static_cast<int>(search);
    p.minimum_power = read_double(object, 5);
    p.stf_phases = read_complex128(PyTuple_GET_ITEM(object, 6), "stf phases",
                                   kStfPeriod * kStfPeriod);
    p.stf_reference_energy = read_double(object, 7);
    p.ltf_reference = read_complex128(PyTuple_GET_ITEM(object, 8), "ltf reference", 64);
    p.ltf_reference_power = read_double(object, 9);
    return p;
}

PyObject* legacy_tuple(const std::vector<LegacyCandidate>& found) {
    PyObject* tuple = PyTuple_New(static_cast<Py_ssize_t>(found.size()));
    if (tuple == nullptr) return nullptr;
    for (size_t i = 0; i < found.size(); ++i) {
        const auto& c = found[i];
        PyObject* item = Py_BuildValue("(nndddd)", static_cast<Py_ssize_t>(c.start),
                                       static_cast<Py_ssize_t>(c.useful_start), c.cfo,
                                       c.amplitude, c.metric, c.confidence);
        if (item == nullptr) {
            Py_DECREF(tuple);
            return nullptr;
        }
        PyTuple_SET_ITEM(tuple, static_cast<Py_ssize_t>(i), item);
    }
    return tuple;
}

PyObject* v5_tuple(const std::vector<V5Candidate>& found) {
    PyObject* tuple = PyTuple_New(static_cast<Py_ssize_t>(found.size()));
    if (tuple == nullptr) return nullptr;
    for (size_t i = 0; i < found.size(); ++i) {
        const auto& c = found[i];
        PyObject* item = Py_BuildValue("(nnddddd)", static_cast<Py_ssize_t>(c.stf_start),
                                       static_cast<Py_ssize_t>(c.ltf_start), c.cfo, c.coarse,
                                       c.metric, c.stf_confidence, c.confidence);
        if (item == nullptr) {
            Py_DECREF(tuple);
            return nullptr;
        }
        PyTuple_SET_ITEM(tuple, static_cast<Py_ssize_t>(i), item);
    }
    return tuple;
}

// acquire(samples, legacy, v5) -> (legacy candidates, v5 candidates)
//
// samples: finite complex64 buffer; legacy/v5: parameter tuples or None.
// Legacy candidates are (start, useful_start, cfo, amplitude, metric,
// confidence) after separated_candidates; v5 ones are (stf_start, ltf_start,
// cfo, coarse_cfo, metric, stf_confidence, confidence) after
// separated_v5_candidates.  Both by start.
PyObject* native_acquire(PyObject*, PyObject* args) {
    PyObject* samples_object = nullptr;
    PyObject* legacy_object = nullptr;
    PyObject* v5_object = nullptr;
    if (!PyArg_ParseTuple(args, "OOO", &samples_object, &legacy_object, &v5_object)) {
        return nullptr;
    }
    try {
        const bool use_legacy = legacy_object != Py_None;
        const bool use_v5 = v5_object != Py_None;
        LegacyParams legacy{};
        V5Params v5{};
        if (use_legacy) legacy = read_legacy(legacy_object);
        if (use_v5) v5 = read_v5(v5_object);
        Buffer buffer(samples_object, "samples");
        if (buffer.bytes() % (2 * static_cast<Py_ssize_t>(sizeof(float))) != 0) {
            throw std::runtime_error("samples must be complex64");
        }
        const long n = static_cast<long>(buffer.bytes() / (2 * sizeof(float)));
        const float* v = static_cast<const float*>(buffer.data());
        std::vector<LegacyCandidate> legacy_found;
        std::vector<V5Candidate> v5_found;
        {
            AllowThreads unlocked;
            bool any = false;
            for (long k = 0; k < 2 * n && !any; ++k) any = v[k] != 0.0F;
            if (n > kHalf && any) {
                const Shared shared = shared_correlation(v, n, kHalf);
                if (use_legacy) legacy_found = legacy_candidates(v, n, shared, legacy);
                if (use_v5) v5_found = v5_candidates(v, n, shared, v5);
            }
        }
        PyObject* first = legacy_tuple(legacy_found);
        if (first == nullptr) return nullptr;
        PyObject* second = v5_tuple(v5_found);
        if (second == nullptr) {
            Py_DECREF(first);
            return nullptr;
        }
        PyObject* result = PyTuple_Pack(2, first, second);
        Py_DECREF(first);
        Py_DECREF(second);
        return result;
    } catch (const std::exception& error) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
}

PyMethodDef methods[] = {
    {"acquire", native_acquire, METH_VARARGS,
     "Legacy and v5 preamble candidates of one complex64 buffer."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_acq_native",
    "Native lag-32 correlation and legacy/v5 preamble acquisition.",
    -1,
    methods,
};

}  // namespace

PyMODINIT_FUNC PyInit__acq_native() {
    return PyModule_Create(&module);
}
