#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

constexpr int kCrcBits = 24;
constexpr uint32_t kCrc24bPolynomial = 0x800063U;
constexpr float kKnownZeroLlr = 64.0F;
constexpr double kNegativeInfinity = -std::numeric_limits<double>::infinity();

struct Plan {
    std::vector<int> block_sizes;
    std::vector<std::pair<int, int>> qpp;
    int filler_bits;
    Py_ssize_t information_bits;

    bool segmented() const { return block_sizes.size() > 1; }
};

struct BlockDecode {
    std::vector<uint8_t> bits;
    int iterations;
    std::string reason;
    bool crc_ok;
};

class AllowThreads {
public:
    AllowThreads() : state_(PyEval_SaveThread()) {}
    ~AllowThreads() { PyEval_RestoreThread(state_); }
    AllowThreads(const AllowThreads&) = delete;
    AllowThreads& operator=(const AllowThreads&) = delete;

private:
    PyThreadState* state_;
};

std::vector<uint8_t> read_byte_vector(PyObject* object, const char* name) {
    Py_buffer view{};
    if (PyObject_GetBuffer(object, &view, PyBUF_CONTIG_RO) != 0) {
        throw std::runtime_error(std::string(name) + " must expose a contiguous byte buffer");
    }
    if (view.itemsize != 1) {
        PyBuffer_Release(&view);
        throw std::runtime_error(std::string(name) + " must contain one-byte values");
    }
    const auto* begin = static_cast<const uint8_t*>(view.buf);
    std::vector<uint8_t> result(begin, begin + view.len);
    PyBuffer_Release(&view);
    return result;
}

std::vector<float> read_float32_bytes(PyObject* object) {
    std::vector<uint8_t> raw = read_byte_vector(object, "observations");
    if (raw.size() % sizeof(float) != 0) {
        throw std::runtime_error("observations byte length must be divisible by four");
    }
    std::vector<float> result(raw.size() / sizeof(float));
    std::memcpy(result.data(), raw.data(), raw.size());
    for (float value : result) {
        if (!std::isfinite(value)) {
            throw std::runtime_error("observations must contain only finite LLRs");
        }
    }
    return result;
}

std::vector<int> read_int_sequence(PyObject* object, const char* name) {
    PyObject* sequence = PySequence_Fast(object, name);
    if (sequence == nullptr) {
        throw std::runtime_error(std::string(name) + " must be a sequence");
    }
    const Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    std::vector<int> result;
    result.reserve(static_cast<size_t>(count));
    for (Py_ssize_t index = 0; index < count; ++index) {
        long value = PyLong_AsLong(PySequence_Fast_GET_ITEM(sequence, index));
        if (PyErr_Occurred()) {
            Py_DECREF(sequence);
            throw std::runtime_error(std::string(name) + " must contain integers");
        }
        if (value <= 0 || value > 6144) {
            Py_DECREF(sequence);
            throw std::runtime_error(std::string(name) + " contains an invalid block size");
        }
        result.push_back(static_cast<int>(value));
    }
    Py_DECREF(sequence);
    if (result.empty()) {
        throw std::runtime_error(std::string(name) + " must not be empty");
    }
    return result;
}

std::vector<std::pair<int, int>> read_qpp_sequence(PyObject* object) {
    PyObject* sequence = PySequence_Fast(object, "qpp_parameters must be a sequence");
    if (sequence == nullptr) {
        throw std::runtime_error("qpp_parameters must be a sequence");
    }
    const Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    std::vector<std::pair<int, int>> result;
    result.reserve(static_cast<size_t>(count));
    for (Py_ssize_t index = 0; index < count; ++index) {
        PyObject* pair = PySequence_Fast(
            PySequence_Fast_GET_ITEM(sequence, index),
            "each QPP entry must contain f1 and f2"
        );
        if (pair == nullptr || PySequence_Fast_GET_SIZE(pair) != 2) {
            Py_XDECREF(pair);
            Py_DECREF(sequence);
            throw std::runtime_error("each QPP entry must contain f1 and f2");
        }
        long f1 = PyLong_AsLong(PySequence_Fast_GET_ITEM(pair, 0));
        long f2 = PyLong_AsLong(PySequence_Fast_GET_ITEM(pair, 1));
        Py_DECREF(pair);
        if (PyErr_Occurred() || f1 <= 0 || f2 <= 0) {
            Py_DECREF(sequence);
            throw std::runtime_error("QPP coefficients must be positive integers");
        }
        result.emplace_back(static_cast<int>(f1), static_cast<int>(f2));
    }
    Py_DECREF(sequence);
    return result;
}

Plan read_plan(
    PyObject* block_sizes,
    int filler_bits,
    PyObject* qpp_parameters,
    Py_ssize_t information_bits
) {
    Plan plan{
        read_int_sequence(block_sizes, "block_sizes"),
        read_qpp_sequence(qpp_parameters),
        filler_bits,
        information_bits,
    };
    if (plan.qpp.size() != plan.block_sizes.size()) {
        throw std::runtime_error("QPP parameter count must match block count");
    }
    if (filler_bits < 0 || filler_bits > plan.block_sizes.front()) {
        throw std::runtime_error("filler_bits is outside the first block");
    }
    const int crc_bits = plan.segmented() ? kCrcBits : 0;
    Py_ssize_t capacity = -filler_bits;
    for (int block_size : plan.block_sizes) {
        capacity += block_size - crc_bits;
    }
    if (capacity != information_bits) {
        throw std::runtime_error("segmentation plan does not match information length");
    }
    return plan;
}

std::vector<int> qpp_permutation(int block_size, int f1, int f2) {
    std::vector<int> permutation(static_cast<size_t>(block_size));
    std::vector<uint8_t> seen(static_cast<size_t>(block_size), 0);
    for (int index = 0; index < block_size; ++index) {
        const int64_t wide_index = index;
        const int value = static_cast<int>(
            (static_cast<int64_t>(f1) * wide_index
             + static_cast<int64_t>(f2) * wide_index * wide_index)
            % block_size
        );
        if (seen[static_cast<size_t>(value)] != 0) {
            throw std::runtime_error("QPP coefficients did not produce a permutation");
        }
        seen[static_cast<size_t>(value)] = 1;
        permutation[static_cast<size_t>(index)] = value;
    }
    return permutation;
}

std::array<uint8_t, kCrcBits> crc24b(const std::vector<uint8_t>& bits, size_t count) {
    uint32_t reg = 0;
    for (size_t index = 0; index < count; ++index) {
        const uint32_t feedback = ((reg >> 23U) & 1U) ^ bits[index];
        reg = (reg << 1U) & 0xFFFFFFU;
        if (feedback != 0) {
            reg ^= kCrc24bPolynomial;
        }
    }
    std::array<uint8_t, kCrcBits> parity{};
    for (int index = 0; index < kCrcBits; ++index) {
        parity[static_cast<size_t>(index)] = static_cast<uint8_t>(
            (reg >> (23 - index)) & 1U
        );
    }
    return parity;
}

bool crc24b_ok(const std::vector<uint8_t>& block) {
    if (block.size() < kCrcBits) {
        return false;
    }
    const size_t data_size = block.size() - kCrcBits;
    const auto expected = crc24b(block, data_size);
    return std::equal(expected.begin(), expected.end(), block.begin() + data_size);
}

uint32_t crc32_ieee(const std::vector<uint8_t>& bytes, size_t count) {
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t index = 0; index < count; ++index) {
        crc ^= bytes[index];
        for (int bit = 0; bit < 8; ++bit) {
            const uint32_t mask = 0U - (crc & 1U);
            crc = (crc >> 1U) ^ (0xEDB88320U & mask);
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

bool outer_crc32_ok(const std::vector<uint8_t>& bits, size_t start = 0) {
    const size_t count = bits.size() - start;
    if (count < 32 || count % 8 != 0) {
        return false;
    }
    std::vector<uint8_t> bytes(count / 8, 0);
    for (size_t index = 0; index < count; ++index) {
        bytes[index / 8] |= static_cast<uint8_t>(
            bits[start + index] << (7U - (index % 8U))
        );
    }
    const size_t protected_size = bytes.size() - 4;
    const uint32_t received =
        (static_cast<uint32_t>(bytes[protected_size]) << 24U)
        | (static_cast<uint32_t>(bytes[protected_size + 1]) << 16U)
        | (static_cast<uint32_t>(bytes[protected_size + 2]) << 8U)
        | static_cast<uint32_t>(bytes[protected_size + 3]);
    return crc32_ieee(bytes, protected_size) == received;
}

struct RscOutput {
    std::vector<uint8_t> parity;
    std::array<uint8_t, 3> tail_systematic;
    std::array<uint8_t, 3> tail_parity;
};

RscOutput rsc_encode(const std::vector<uint8_t>& bits) {
    std::array<uint8_t, 3> state{0, 0, 0};
    RscOutput output{std::vector<uint8_t>(bits.size()), {}, {}};
    for (size_t index = 0; index < bits.size(); ++index) {
        const uint8_t feedback = bits[index] ^ state[1] ^ state[2];
        output.parity[index] = feedback ^ state[0] ^ state[2];
        state = {feedback, state[0], state[1]};
    }
    for (size_t index = 0; index < 3; ++index) {
        const uint8_t input = state[1] ^ state[2];
        const uint8_t feedback = input ^ state[1] ^ state[2];
        output.tail_systematic[index] = input;
        output.tail_parity[index] = feedback ^ state[0] ^ state[2];
        state = {feedback, state[0], state[1]};
    }
    if (state != std::array<uint8_t, 3>{0, 0, 0}) {
        throw std::runtime_error("RSC termination did not reach zero state");
    }
    return output;
}

std::vector<uint8_t> encode_block(
    const std::vector<uint8_t>& block,
    int f1,
    int f2
) {
    const int block_size = static_cast<int>(block.size());
    const std::vector<int> permutation = qpp_permutation(block_size, f1, f2);
    std::vector<uint8_t> interleaved(block.size());
    for (int index = 0; index < block_size; ++index) {
        interleaved[static_cast<size_t>(index)] = block[static_cast<size_t>(permutation[index])];
    }
    const RscOutput first = rsc_encode(block);
    const RscOutput second = rsc_encode(interleaved);
    std::vector<uint8_t> encoded;
    encoded.reserve(3U * (block.size() + 4U));
    for (size_t index = 0; index < block.size(); ++index) {
        encoded.push_back(block[index]);
        encoded.push_back(first.parity[index]);
        encoded.push_back(second.parity[index]);
    }
    for (size_t index = 0; index < 3; ++index) {
        encoded.push_back(first.tail_systematic[index]);
        encoded.push_back(first.tail_parity[index]);
    }
    for (size_t index = 0; index < 3; ++index) {
        encoded.push_back(second.tail_systematic[index]);
        encoded.push_back(second.tail_parity[index]);
    }
    return encoded;
}

std::vector<std::vector<uint8_t>> segment(
    const std::vector<uint8_t>& information,
    const Plan& plan
) {
    const int crc_bits = plan.segmented() ? kCrcBits : 0;
    std::vector<std::vector<uint8_t>> blocks;
    blocks.reserve(plan.block_sizes.size());
    size_t source = 0;
    for (size_t block_index = 0; block_index < plan.block_sizes.size(); ++block_index) {
        const int block_size = plan.block_sizes[block_index];
        std::vector<uint8_t> block(static_cast<size_t>(block_size), 0);
        const int data_start = block_index == 0 ? plan.filler_bits : 0;
        const int data_end = block_size - crc_bits;
        for (int index = data_start; index < data_end; ++index) {
            block[static_cast<size_t>(index)] = information[source++];
        }
        if (plan.segmented()) {
            const auto parity = crc24b(block, static_cast<size_t>(data_end));
            std::copy(parity.begin(), parity.end(), block.begin() + data_end);
        }
        blocks.push_back(std::move(block));
    }
    if (source != information.size()) {
        throw std::runtime_error("segmentation did not consume all information bits");
    }
    return blocks;
}

struct Trellis {
    std::array<std::array<int, 2>, 8> next_state{};
    std::array<std::array<uint8_t, 2>, 8> parity{};
};

Trellis make_trellis() {
    Trellis trellis;
    for (int state = 0; state < 8; ++state) {
        const int s0 = state & 1;
        const int s1 = (state >> 1) & 1;
        const int s2 = (state >> 2) & 1;
        for (int input = 0; input < 2; ++input) {
            const int feedback = input ^ s1 ^ s2;
            trellis.parity[state][input] = static_cast<uint8_t>(feedback ^ s0 ^ s2);
            trellis.next_state[state][input] = feedback | (s0 << 1) | (s1 << 2);
        }
    }
    return trellis;
}

double branch_metric(int input, int parity, double systematic, double parity_llr, double prior) {
    return 0.5 * (
        (1.0 - 2.0 * input) * (systematic + prior)
        + (1.0 - 2.0 * parity) * parity_llr
    );
}

void normalize(std::array<double, 8>& values) {
    const double maximum = *std::max_element(values.begin(), values.end());
    if (std::isfinite(maximum)) {
        for (double& value : values) {
            value -= maximum;
        }
    }
}

std::pair<std::vector<double>, std::vector<double>> constituent_max_log_map(
    const std::vector<double>& systematic,
    const std::vector<double>& parity,
    const std::vector<double>& prior
) {
    const size_t block_size = prior.size();
    const size_t steps = block_size + 3;
    if (systematic.size() != steps || parity.size() != steps) {
        throw std::runtime_error("constituent observation length mismatch");
    }
    static const Trellis trellis = make_trellis();
    std::vector<std::array<double, 8>> alpha(steps + 1);
    std::vector<std::array<double, 8>> beta(steps + 1);
    for (auto& row : alpha) {
        row.fill(kNegativeInfinity);
    }
    for (auto& row : beta) {
        row.fill(kNegativeInfinity);
    }
    alpha[0][0] = 0.0;
    beta[steps][0] = 0.0;

    for (size_t time = 0; time < steps; ++time) {
        const double a_priori = time < block_size ? prior[time] : 0.0;
        for (int state = 0; state < 8; ++state) {
            if (!std::isfinite(alpha[time][state])) {
                continue;
            }
            for (int input = 0; input < 2; ++input) {
                const int destination = trellis.next_state[state][input];
                const double value = alpha[time][state] + branch_metric(
                    input,
                    trellis.parity[state][input],
                    systematic[time],
                    parity[time],
                    a_priori
                );
                alpha[time + 1][destination] = std::max(
                    alpha[time + 1][destination], value
                );
            }
        }
        normalize(alpha[time + 1]);
    }

    for (size_t reverse = steps; reverse > 0; --reverse) {
        const size_t time = reverse - 1;
        const double a_priori = time < block_size ? prior[time] : 0.0;
        for (int state = 0; state < 8; ++state) {
            for (int input = 0; input < 2; ++input) {
                const int destination = trellis.next_state[state][input];
                const double value = branch_metric(
                    input,
                    trellis.parity[state][input],
                    systematic[time],
                    parity[time],
                    a_priori
                ) + beta[time + 1][destination];
                beta[time][state] = std::max(beta[time][state], value);
            }
        }
        normalize(beta[time]);
    }

    std::vector<double> posterior(block_size);
    std::vector<double> extrinsic(block_size);
    for (size_t time = 0; time < block_size; ++time) {
        std::array<double, 2> hypothesis{kNegativeInfinity, kNegativeInfinity};
        for (int state = 0; state < 8; ++state) {
            for (int input = 0; input < 2; ++input) {
                const int destination = trellis.next_state[state][input];
                const double value = alpha[time][state]
                    + branch_metric(
                        input,
                        trellis.parity[state][input],
                        systematic[time],
                        parity[time],
                        prior[time]
                    )
                    + beta[time + 1][destination];
                hypothesis[input] = std::max(hypothesis[input], value);
            }
        }
        posterior[time] = hypothesis[0] - hypothesis[1];
        extrinsic[time] = std::clamp(
            posterior[time] - systematic[time] - prior[time],
            -64.0,
            64.0
        );
    }
    return {std::move(posterior), std::move(extrinsic)};
}

BlockDecode decode_block(
    const std::vector<float>& llrs,
    int block_size,
    int f1,
    int f2,
    int filler_bits,
    int max_iterations,
    bool segmented
) {
    const size_t expected = static_cast<size_t>(3 * (block_size + 4));
    if (llrs.size() != expected) {
        throw std::runtime_error("Turbo block observation length mismatch");
    }
    const std::vector<int> permutation = qpp_permutation(block_size, f1, f2);
    std::vector<double> sys_one(static_cast<size_t>(block_size + 3));
    std::vector<double> par_one(static_cast<size_t>(block_size + 3));
    std::vector<double> sys_two(static_cast<size_t>(block_size + 3));
    std::vector<double> par_two(static_cast<size_t>(block_size + 3));
    for (int index = 0; index < block_size; ++index) {
        sys_one[static_cast<size_t>(index)] = llrs[static_cast<size_t>(3 * index)];
        par_one[static_cast<size_t>(index)] = llrs[static_cast<size_t>(3 * index + 1)];
        sys_two[static_cast<size_t>(index)] = llrs[
            static_cast<size_t>(3 * permutation[static_cast<size_t>(index)])
        ];
        par_two[static_cast<size_t>(index)] = llrs[static_cast<size_t>(3 * index + 2)];
    }
    const size_t tail = static_cast<size_t>(3 * block_size);
    for (int index = 0; index < 3; ++index) {
        sys_one[static_cast<size_t>(block_size + index)] = llrs[tail + 2U * index];
        par_one[static_cast<size_t>(block_size + index)] = llrs[tail + 2U * index + 1U];
        sys_two[static_cast<size_t>(block_size + index)] = llrs[tail + 6U + 2U * index];
        par_two[static_cast<size_t>(block_size + index)] = llrs[tail + 7U + 2U * index];
    }

    std::vector<double> prior_one(static_cast<size_t>(block_size), 0.0);
    for (int index = 0; index < filler_bits; ++index) {
        prior_one[static_cast<size_t>(index)] = kKnownZeroLlr;
    }
    std::vector<uint8_t> candidate(static_cast<size_t>(block_size), 0);
    bool crc_ok = false;
    std::string reason = "max_iterations";
    int used_iterations = max_iterations;
    for (int iteration = 1; iteration <= max_iterations; ++iteration) {
        auto first = constituent_max_log_map(sys_one, par_one, prior_one);
        std::vector<double> prior_two(static_cast<size_t>(block_size));
        for (int index = 0; index < block_size; ++index) {
            prior_two[static_cast<size_t>(index)] = first.second[
                static_cast<size_t>(permutation[static_cast<size_t>(index)])
            ];
            if (permutation[static_cast<size_t>(index)] < filler_bits) {
                prior_two[static_cast<size_t>(index)] += kKnownZeroLlr;
            }
        }
        auto second = constituent_max_log_map(sys_two, par_two, prior_two);
        for (int index = 0; index < block_size; ++index) {
            candidate[static_cast<size_t>(permutation[static_cast<size_t>(index)])] =
                static_cast<uint8_t>(second.first[static_cast<size_t>(index)] < 0.0);
        }

        crc_ok = segmented ? crc24b_ok(candidate)
                           : outer_crc32_ok(candidate, static_cast<size_t>(filler_bits));
        if (crc_ok) {
            used_iterations = iteration;
            reason = segmented ? "code_block_crc24b" : "outer_crc32";
            break;
        }
        prior_one.assign(static_cast<size_t>(block_size), 0.0);
        for (int index = 0; index < block_size; ++index) {
            prior_one[static_cast<size_t>(permutation[static_cast<size_t>(index)])] =
                second.second[static_cast<size_t>(index)];
        }
        for (int index = 0; index < filler_bits; ++index) {
            prior_one[static_cast<size_t>(index)] += kKnownZeroLlr;
        }
    }
    return {std::move(candidate), used_iterations, std::move(reason), crc_ok};
}

// --- float32 Max-Log-MAP kernel ----------------------------------------------
//
// The same decoder as decode_block (same trellis, branch metric, extrinsic
// clip, CRC early stop and iteration loop), in float32 with every buffer
// kept per thread across calls.  The 8-state recursions are written as the
// LTE trellis' butterflies: destination states 2j and 2j+1 have the
// predecessors j and j+4, so each step is two 4-wide max-plus operations on
// contiguous halves with constant branch-metric signs, which compilers turn
// into SIMD on any target (SSE2 on baseline x86-64, NEON on aarch64) with no
// intrinsics, no ISA flag and no branches.  decode_block (double) stays as
// the reference kernel.

constexpr float kNegativeInfinityF = -std::numeric_limits<float>::infinity();

// Branch metric signs per butterfly lane j (0..3).  For predecessor state
// s = j + 4 s2 and feedback bit fb (= destination & 1): input u = fb ^ s1 ^ s2
// and parity p = fb ^ s0 ^ s2, with s0 = j & 1, s1 = (j >> 1) & 1.
struct ButterflySigns {
    // [s2][fb][j] -> +1 for bit 0, -1 for bit 1
    float input[2][2][4];
    float parity[2][2][4];
    ButterflySigns() {
        for (int s2 = 0; s2 < 2; ++s2) {
            for (int fb = 0; fb < 2; ++fb) {
                for (int j = 0; j < 4; ++j) {
                    const int s0 = j & 1;
                    const int s1 = (j >> 1) & 1;
                    const int u = fb ^ s1 ^ s2;
                    const int p = fb ^ s0 ^ s2;
                    input[s2][fb][j] = u ? -1.0F : 1.0F;
                    parity[s2][fb][j] = p ? -1.0F : 1.0F;
                }
            }
        }
    }
};

const ButterflySigns& signs() {
    static const ButterflySigns table;
    return table;
}

struct FastWorkspace {
    std::vector<float> sys_one, par_one, sys_two, par_two;
    std::vector<float> prior_one, prior_two, extrinsic, posterior;
    std::vector<float> alpha;  // (steps + 1) x 8
    std::vector<uint8_t> candidate, previous;
    std::vector<int> permutation;
    int permutation_size = 0, permutation_f1 = 0, permutation_f2 = 0;

    void reserve(size_t block_size) {
        const size_t steps = block_size + 3;
        for (auto* v : {&sys_one, &par_one, &sys_two, &par_two}) v->resize(steps);
        for (auto* v : {&prior_one, &prior_two, &extrinsic, &posterior}) v->resize(block_size);
        alpha.resize((steps + 1) * 8);
        candidate.resize(block_size);
        previous.resize(block_size);
    }

    const std::vector<int>& qpp(int block_size, int f1, int f2) {
        if (permutation_size != block_size || permutation_f1 != f1 || permutation_f2 != f2) {
            permutation = qpp_permutation(block_size, f1, f2);
            permutation_size = block_size;
            permutation_f1 = f1;
            permutation_f2 = f2;
        }
        return permutation;
    }
};

FastWorkspace& workspace() {
    thread_local FastWorkspace instance;
    return instance;
}

inline void normalize8(float* values) {
    float maximum = values[0];
    for (int k = 1; k < 8; ++k) maximum = std::max(maximum, values[k]);
    if (std::isfinite(maximum)) {
        for (int k = 0; k < 8; ++k) values[k] -= maximum;
    }
}

// One constituent decoder: writes posterior and clipped extrinsic LLRs.
void constituent_fast(
    const float* systematic,
    const float* parity,
    const float* prior,
    size_t block_size,
    FastWorkspace& w,
    float* posterior,
    float* extrinsic
) {
    const ButterflySigns& sg = signs();
    const size_t steps = block_size + 3;
    float* alpha = w.alpha.data();
    for (int k = 0; k < 8; ++k) alpha[k] = kNegativeInfinityF;
    alpha[0] = 0.0F;

    for (size_t time = 0; time < steps; ++time) {
        const float a_priori = time < block_size ? prior[time] : 0.0F;
        const float a = 0.5F * (systematic[time] + a_priori);
        const float b = 0.5F * parity[time];
        const float* current = alpha + time * 8;
        float* next = alpha + (time + 1) * 8;
        float even[4], odd[4];
        for (int j = 0; j < 4; ++j) {
            const float from_low = current[j];
            const float from_high = current[j + 4];
            const float e0 = from_low + (sg.input[0][0][j] * a + sg.parity[0][0][j] * b);
            const float e1 = from_high + (sg.input[1][0][j] * a + sg.parity[1][0][j] * b);
            const float o0 = from_low + (sg.input[0][1][j] * a + sg.parity[0][1][j] * b);
            const float o1 = from_high + (sg.input[1][1][j] * a + sg.parity[1][1][j] * b);
            even[j] = std::max(e0, e1);
            odd[j] = std::max(o0, o1);
        }
        for (int j = 0; j < 4; ++j) {
            next[2 * j] = even[j];
            next[2 * j + 1] = odd[j];
        }
        normalize8(next);
    }

    float beta[8];
    for (int k = 0; k < 8; ++k) beta[k] = kNegativeInfinityF;
    beta[0] = 0.0F;
    // The three tail steps carry no information bit and no prior.
    for (size_t time = steps; time-- > block_size;) {
        const float a = 0.5F * systematic[time];
        const float b = 0.5F * parity[time];
        float updated[8];
        for (int s2 = 0; s2 < 2; ++s2) {
            for (int j = 0; j < 4; ++j) {
                const float via_even =
                    (sg.input[s2][0][j] * a + sg.parity[s2][0][j] * b) + beta[2 * j];
                const float via_odd =
                    (sg.input[s2][1][j] * a + sg.parity[s2][1][j] * b) + beta[2 * j + 1];
                updated[j + 4 * s2] = std::max(via_even, via_odd);
            }
        }
        for (int k = 0; k < 8; ++k) beta[k] = updated[k];
        normalize8(beta);
    }
    // Information steps: the beta update and the bit LLR share every sum.
    // For predecessor j + 4 s2 the branch into destination 2j + fb carries
    // input bit fb ^ s1 ^ s2, so the "bit 0" path is the even branch exactly
    // where s1 == s2 (zero_even below).
    for (size_t time = block_size; time-- > 0;) {
        const float a = 0.5F * (systematic[time] + prior[time]);
        const float b = 0.5F * parity[time];
        const float* current = alpha + time * 8;
        float updated[8], zero[8], one[8];
        for (int s2 = 0; s2 < 2; ++s2) {
            for (int j = 0; j < 4; ++j) {
                const float via_even =
                    (sg.input[s2][0][j] * a + sg.parity[s2][0][j] * b) + beta[2 * j];
                const float via_odd =
                    (sg.input[s2][1][j] * a + sg.parity[s2][1][j] * b) + beta[2 * j + 1];
                updated[j + 4 * s2] = std::max(via_even, via_odd);
                const bool zero_even = ((j >> 1) & 1) == s2;
                const float path_even = current[j + 4 * s2] + via_even;
                const float path_odd = current[j + 4 * s2] + via_odd;
                zero[j + 4 * s2] = zero_even ? path_even : path_odd;
                one[j + 4 * s2] = zero_even ? path_odd : path_even;
            }
        }
        float best0 = zero[0], best1 = one[0];
        for (int k = 1; k < 8; ++k) {
            best0 = std::max(best0, zero[k]);
            best1 = std::max(best1, one[k]);
        }
        const float value = best0 - best1;
        posterior[time] = value;
        extrinsic[time] = std::clamp(value - systematic[time] - prior[time], -64.0F, 64.0F);
        for (int k = 0; k < 8; ++k) beta[k] = updated[k];
        normalize8(beta);
    }
}

BlockDecode decode_block_fast(
    const float* llrs,
    int block_size,
    int f1,
    int f2,
    int filler_bits,
    int max_iterations,
    bool segmented,
    int give_up_after
) {
    FastWorkspace& w = workspace();
    const size_t k = static_cast<size_t>(block_size);
    w.reserve(k);
    const std::vector<int>& permutation = w.qpp(block_size, f1, f2);
    for (size_t index = 0; index < k; ++index) {
        w.sys_one[index] = llrs[3 * index];
        w.par_one[index] = llrs[3 * index + 1];
        w.sys_two[index] = llrs[3 * static_cast<size_t>(permutation[index])];
        w.par_two[index] = llrs[3 * index + 2];
    }
    const size_t tail = 3 * k;
    for (size_t index = 0; index < 3; ++index) {
        w.sys_one[k + index] = llrs[tail + 2 * index];
        w.par_one[k + index] = llrs[tail + 2 * index + 1];
        w.sys_two[k + index] = llrs[tail + 6 + 2 * index];
        w.par_two[k + index] = llrs[tail + 7 + 2 * index];
    }
    std::fill(w.prior_one.begin(), w.prior_one.end(), 0.0F);
    for (int index = 0; index < filler_bits; ++index) {
        w.prior_one[static_cast<size_t>(index)] = kKnownZeroLlr;
    }
    std::fill(w.candidate.begin(), w.candidate.end(), 0);
    bool crc_ok = false;
    std::string reason = "max_iterations";
    int used_iterations = max_iterations;
    for (int iteration = 1; iteration <= max_iterations; ++iteration) {
        constituent_fast(w.sys_one.data(), w.par_one.data(), w.prior_one.data(), k, w,
                         w.posterior.data(), w.extrinsic.data());
        for (size_t index = 0; index < k; ++index) {
            const int source = permutation[index];
            w.prior_two[index] = w.extrinsic[static_cast<size_t>(source)]
                + (source < filler_bits ? kKnownZeroLlr : 0.0F);
        }
        constituent_fast(w.sys_two.data(), w.par_two.data(), w.prior_two.data(), k, w,
                         w.posterior.data(), w.extrinsic.data());
        w.previous.swap(w.candidate);
        size_t changed = 0;
        for (size_t index = 0; index < k; ++index) {
            const uint8_t bit = static_cast<uint8_t>(w.posterior[index] < 0.0F);
            const size_t target = static_cast<size_t>(permutation[index]);
            w.candidate[target] = bit;
        }
        for (size_t index = 0; index < k; ++index) {
            changed += static_cast<size_t>(w.candidate[index] != w.previous[index]);
        }
        crc_ok = segmented ? crc24b_ok(w.candidate)
                           : outer_crc32_ok(w.candidate, static_cast<size_t>(filler_bits));
        if (crc_ok) {
            used_iterations = iteration;
            reason = segmented ? "code_block_crc24b" : "outer_crc32";
            break;
        }
        // Opt-in overload protection: a block whose hard decisions still move
        // in more than 1/64 of its bits this late is treated as hopeless.
        if (give_up_after > 0 && iteration >= give_up_after && iteration < max_iterations
            && changed * 64 > k) {
            used_iterations = iteration;
            reason = "abandoned";
            break;
        }
        std::fill(w.prior_one.begin(), w.prior_one.end(), 0.0F);
        for (size_t index = 0; index < k; ++index) {
            w.prior_one[static_cast<size_t>(permutation[index])] = w.extrinsic[index];
        }
        for (int index = 0; index < filler_bits; ++index) {
            w.prior_one[static_cast<size_t>(index)] += kKnownZeroLlr;
        }
    }
    return {std::vector<uint8_t>(w.candidate.begin(), w.candidate.end()), used_iterations,
            std::move(reason), crc_ok};
}

PyObject* bytes_from_bits(const std::vector<uint8_t>& bits) {
    return PyBytes_FromStringAndSize(
        reinterpret_cast<const char*>(bits.data()),
        static_cast<Py_ssize_t>(bits.size())
    );
}

PyObject* tuple_from_ints(const std::vector<int>& values) {
    PyObject* tuple = PyTuple_New(static_cast<Py_ssize_t>(values.size()));
    if (tuple == nullptr) {
        return nullptr;
    }
    for (size_t index = 0; index < values.size(); ++index) {
        PyTuple_SET_ITEM(tuple, static_cast<Py_ssize_t>(index), PyLong_FromLong(values[index]));
    }
    return tuple;
}

PyObject* tuple_from_strings(const std::vector<std::string>& values) {
    PyObject* tuple = PyTuple_New(static_cast<Py_ssize_t>(values.size()));
    if (tuple == nullptr) {
        return nullptr;
    }
    for (size_t index = 0; index < values.size(); ++index) {
        PyTuple_SET_ITEM(
            tuple,
            static_cast<Py_ssize_t>(index),
            PyUnicode_FromString(values[index].c_str())
        );
    }
    return tuple;
}

PyObject* tuple_from_crc(const std::vector<bool>& values, bool segmented) {
    PyObject* tuple = PyTuple_New(static_cast<Py_ssize_t>(values.size()));
    if (tuple == nullptr) {
        return nullptr;
    }
    for (size_t index = 0; index < values.size(); ++index) {
        PyObject* value = segmented ? (values[index] ? Py_True : Py_False) : Py_None;
        Py_INCREF(value);
        PyTuple_SET_ITEM(tuple, static_cast<Py_ssize_t>(index), value);
    }
    return tuple;
}

void set_dict_item(PyObject* dict, const char* key, PyObject* value) {
    if (value == nullptr || PyDict_SetItemString(dict, key, value) != 0) {
        Py_XDECREF(value);
        throw std::runtime_error("failed to construct native decode report");
    }
    Py_DECREF(value);
}

PyObject* native_encode(PyObject*, PyObject* args) {
    PyObject* information_object = nullptr;
    PyObject* block_sizes_object = nullptr;
    PyObject* qpp_object = nullptr;
    int filler_bits = 0;
    if (!PyArg_ParseTuple(
            args,
            "OOiO",
            &information_object,
            &block_sizes_object,
            &filler_bits,
            &qpp_object
        )) {
        return nullptr;
    }
    try {
        std::vector<uint8_t> information = read_byte_vector(
            information_object, "information_bits"
        );
        for (uint8_t bit : information) {
            if (bit > 1) {
                throw std::runtime_error("information_bits must contain only 0 and 1");
            }
        }
        const Plan plan = read_plan(
            block_sizes_object,
            filler_bits,
            qpp_object,
            static_cast<Py_ssize_t>(information.size())
        );
        std::vector<uint8_t> encoded;
        {
            AllowThreads allow_threads;
            const auto blocks = segment(information, plan);
            for (size_t index = 0; index < blocks.size(); ++index) {
                auto block = encode_block(
                    blocks[index], plan.qpp[index].first, plan.qpp[index].second
                );
                encoded.insert(encoded.end(), block.begin(), block.end());
            }
        }
        return bytes_from_bits(encoded);
    } catch (const std::exception& error) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_ValueError, error.what());
        }
        return nullptr;
    }
}

PyObject* decode_frame(PyObject* args, bool fast) {
    PyObject* observations_object = nullptr;
    PyObject* block_sizes_object = nullptr;
    PyObject* qpp_object = nullptr;
    Py_ssize_t information_bits = 0;
    int filler_bits = 0;
    int max_iterations = 0;
    int give_up_after = 0;
    if (!PyArg_ParseTuple(
            args,
            "OnOiOi|i",
            &observations_object,
            &information_bits,
            &block_sizes_object,
            &filler_bits,
            &qpp_object,
            &max_iterations,
            &give_up_after
        )) {
        return nullptr;
    }
    try {
        if (give_up_after < 0) {
            throw std::runtime_error("give_up_after must be non-negative");
        }
        if (give_up_after != 0 && !fast) {
            throw std::runtime_error("give_up_after needs the float32 kernel");
        }
        if (information_bits < 0) {
            throw std::runtime_error("information_bit_count must be non-negative");
        }
        if (max_iterations != 2 && max_iterations != 4
            && max_iterations != 6 && max_iterations != 8) {
            throw std::runtime_error("max_iterations must be one of 2, 4, 6, or 8");
        }
        const Plan plan = read_plan(
            block_sizes_object,
            filler_bits,
            qpp_object,
            information_bits
        );
        const std::vector<float> observations = read_float32_bytes(observations_object);
        size_t expected = 0;
        for (int block_size : plan.block_sizes) {
            expected += static_cast<size_t>(3 * (block_size + 4));
        }
        if (observations.size() != expected) {
            throw std::runtime_error("observations length does not match segmentation plan");
        }

        std::vector<std::vector<uint8_t>> decoded_blocks;
        std::vector<int> iterations;
        std::vector<std::string> reasons;
        std::vector<bool> crc_outcomes;
        decoded_blocks.reserve(plan.block_sizes.size());
        size_t offset = 0;
        {
            AllowThreads allow_threads;
            for (size_t index = 0; index < plan.block_sizes.size(); ++index) {
                const int block_size = plan.block_sizes[index];
                const size_t coded_size = static_cast<size_t>(3 * (block_size + 4));
                BlockDecode decoded;
                if (fast) {
                    decoded = decode_block_fast(
                        observations.data() + offset,
                        block_size,
                        plan.qpp[index].first,
                        plan.qpp[index].second,
                        index == 0 ? plan.filler_bits : 0,
                        max_iterations,
                        plan.segmented(),
                        give_up_after
                    );
                } else {
                    const std::vector<float> block_llrs(
                        observations.begin() + static_cast<ptrdiff_t>(offset),
                        observations.begin() + static_cast<ptrdiff_t>(offset + coded_size)
                    );
                    decoded = decode_block(
                        block_llrs,
                        block_size,
                        plan.qpp[index].first,
                        plan.qpp[index].second,
                        index == 0 ? plan.filler_bits : 0,
                        max_iterations,
                        plan.segmented()
                    );
                }
                offset += coded_size;
                iterations.push_back(decoded.iterations);
                reasons.push_back(decoded.reason);
                crc_outcomes.push_back(decoded.crc_ok);
                decoded_blocks.push_back(std::move(decoded.bits));
            }
        }

        bool filler_ok = true;
        for (int index = 0; index < plan.filler_bits; ++index) {
            filler_ok = filler_ok && decoded_blocks[0][static_cast<size_t>(index)] == 0;
        }
        const int crc_bits = plan.segmented() ? kCrcBits : 0;
        std::vector<uint8_t> information;
        information.reserve(static_cast<size_t>(information_bits));
        for (size_t block_index = 0; block_index < decoded_blocks.size(); ++block_index) {
            const auto& block = decoded_blocks[block_index];
            const size_t begin = block_index == 0 ? static_cast<size_t>(plan.filler_bits) : 0U;
            const size_t end = block.size() - static_cast<size_t>(crc_bits);
            information.insert(information.end(), block.begin() + begin, block.begin() + end);
        }
        if (information.size() != static_cast<size_t>(information_bits)) {
            throw std::runtime_error("decoded information length mismatch");
        }

        PyObject* report = PyDict_New();
        if (report == nullptr) {
            throw std::runtime_error("failed to allocate native decode report");
        }
        try {
            set_dict_item(report, "configured_max_iterations", PyLong_FromLong(max_iterations));
            set_dict_item(report, "actual_iterations_per_block", tuple_from_ints(iterations));
            set_dict_item(report, "early_stop_reasons", tuple_from_strings(reasons));
            set_dict_item(
                report,
                "code_block_crc_ok",
                tuple_from_crc(crc_outcomes, plan.segmented())
            );
            PyObject* outer = plan.segmented()
                ? Py_NewRef(Py_None)
                : Py_NewRef(crc_outcomes[0] ? Py_True : Py_False);
            set_dict_item(report, "outer_crc_ok", outer);
            set_dict_item(
                report,
                "block_count",
                PyLong_FromSize_t(plan.block_sizes.size())
            );
            set_dict_item(report, "filler_bit_count", PyLong_FromLong(plan.filler_bits));
            set_dict_item(report, "filler_ok", Py_NewRef(filler_ok ? Py_True : Py_False));
        } catch (...) {
            Py_DECREF(report);
            throw;
        }
        PyObject* decoded_bytes = bytes_from_bits(information);
        if (decoded_bytes == nullptr) {
            Py_DECREF(report);
            return nullptr;
        }
        PyObject* result = PyTuple_Pack(2, decoded_bytes, report);
        Py_DECREF(decoded_bytes);
        Py_DECREF(report);
        return result;
    } catch (const std::exception& error) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_ValueError, error.what());
        }
        return nullptr;
    }
}

PyObject* native_decode(PyObject*, PyObject* args) { return decode_frame(args, true); }

PyObject* native_decode_reference(PyObject*, PyObject* args) {
    return decode_frame(args, false);
}

// --- Terminated K=7 rate-1/N convolutional Viterbi ---------------------------
//
// GNU Radio's cc_decoder only builds k=7 rate-1/2 graphs, so the rate-1/3
// mother code needs its own decoder.  Inputs are project-convention LLRs where
// a positive value means bit 0; the branch metric is the correlation between
// the expected signs and those LLRs, so no scaling or biasing is involved.

constexpr int kViterbiConstraint = 7;
constexpr int kViterbiStates = 1 << (kViterbiConstraint - 1);
constexpr int kViterbiTailBits = kViterbiConstraint - 1;

std::vector<uint8_t> viterbi_decode(
    const std::vector<float>& llrs,
    const std::vector<int>& generators
) {
    const int outputs = static_cast<int>(generators.size());
    if (outputs < 2) {
        throw std::runtime_error("convolutional code needs at least two generators");
    }
    if (llrs.size() % static_cast<size_t>(outputs) != 0) {
        throw std::runtime_error("observation count must be a multiple of the rate");
    }
    const size_t steps = llrs.size() / static_cast<size_t>(outputs);
    if (steps <= static_cast<size_t>(kViterbiTailBits)) {
        throw std::runtime_error("observation count is shorter than the trellis tail");
    }

    // expected[state][input][output] as a +1/-1 sign, matching bit 0 -> +1.
    std::vector<float> expected(
        static_cast<size_t>(kViterbiStates) * 2U * static_cast<size_t>(outputs)
    );
    for (int state = 0; state < kViterbiStates; ++state) {
        for (int input = 0; input < 2; ++input) {
            const int reg = (state << 1) | input;
            for (int output = 0; output < outputs; ++output) {
                const int parity = __builtin_parity(
                    static_cast<unsigned>(reg & generators[static_cast<size_t>(output)])
                );
                const size_t slot =
                    (static_cast<size_t>(state) * 2U + static_cast<size_t>(input))
                        * static_cast<size_t>(outputs)
                    + static_cast<size_t>(output);
                expected[slot] = parity ? -1.0f : 1.0f;
            }
        }
    }

    constexpr float kInfinity = 1e30f;
    std::vector<float> metrics(kViterbiStates, kInfinity);
    metrics[0] = 0.0f;
    std::vector<float> next(kViterbiStates);
    std::vector<uint8_t> survivors(steps * static_cast<size_t>(kViterbiStates));

    for (size_t step = 0; step < steps; ++step) {
        const float* observation = llrs.data() + step * static_cast<size_t>(outputs);
        for (int destination = 0; destination < kViterbiStates; ++destination) {
            const int input = destination & 1;
            const int predecessor_a = destination >> 1;
            const int predecessor_b = predecessor_a | (kViterbiStates >> 1);
            float branch_a = 0.0f;
            float branch_b = 0.0f;
            for (int output = 0; output < outputs; ++output) {
                const size_t slot_a =
                    (static_cast<size_t>(predecessor_a) * 2U + static_cast<size_t>(input))
                        * static_cast<size_t>(outputs)
                    + static_cast<size_t>(output);
                const size_t slot_b =
                    (static_cast<size_t>(predecessor_b) * 2U + static_cast<size_t>(input))
                        * static_cast<size_t>(outputs)
                    + static_cast<size_t>(output);
                branch_a -= expected[slot_a] * observation[output];
                branch_b -= expected[slot_b] * observation[output];
            }
            const float candidate_a = metrics[static_cast<size_t>(predecessor_a)] + branch_a;
            const float candidate_b = metrics[static_cast<size_t>(predecessor_b)] + branch_b;
            const bool choose_a = candidate_a <= candidate_b;
            next[static_cast<size_t>(destination)] = choose_a ? candidate_a : candidate_b;
            survivors[step * static_cast<size_t>(kViterbiStates)
                      + static_cast<size_t>(destination)] =
                static_cast<uint8_t>(choose_a ? predecessor_a : predecessor_b);
        }
        metrics.swap(next);
    }

    // The encoder is terminated, so the survivor path ends in the zero state.
    std::vector<uint8_t> decoded(steps);
    int state = 0;
    for (size_t index = steps; index-- > 0;) {
        decoded[index] = static_cast<uint8_t>(state & 1);
        state = static_cast<int>(
            survivors[index * static_cast<size_t>(kViterbiStates) + static_cast<size_t>(state)]
        );
    }
    return decoded;
}

PyObject* native_viterbi(PyObject*, PyObject* args) {
    PyObject* observations_object = nullptr;
    PyObject* generators_object = nullptr;
    if (!PyArg_ParseTuple(args, "OO", &observations_object, &generators_object)) {
        return nullptr;
    }
    try {
        const std::vector<float> llrs = read_float32_bytes(observations_object);
        const std::vector<int> generators =
            read_int_sequence(generators_object, "generators");
        std::vector<uint8_t> decoded;
        {
            AllowThreads unlocked;
            decoded = viterbi_decode(llrs, generators);
        }
        return bytes_from_bits(decoded);
    } catch (const std::exception& error) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_ValueError, error.what());
        }
        return nullptr;
    }
}

PyMethodDef methods[] = {
    {"encode", native_encode, METH_VARARGS, "Encode one complete information frame."},
    {"decode", native_decode, METH_VARARGS,
     "Decode one complete positive-for-zero LLR frame (float32 kernel; optional "
     "give_up_after iteration for hopeless blocks, 0 = never)."},
    {"decode_reference", native_decode_reference, METH_VARARGS,
     "Decode one frame with the original double-precision reference kernel."},
    {"viterbi", native_viterbi, METH_VARARGS, "Decode one terminated rate-1/N convolutional frame."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_turbo_native",
    "Private LTE-derived Turbo codec and rate-1/N convolutional Viterbi.",
    -1,
    methods,
};

}  // namespace

PyMODINIT_FUNC PyInit__turbo_native() {
    return PyModule_Create(&module);
}
