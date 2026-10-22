#pragma once

#include <charconv>
#include <cstdlib>
#include <string>
#include <vector>
#include <stdexcept>

namespace range_parser {

inline std::string trim_copy(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

// Strict integer: -?digits, consumed fully, within int range.
inline bool parse_range_int(const std::string& text, int& out) {
    const std::string t = trim_copy(text);
    const char* last = t.data() + t.size();
    const auto result = std::from_chars(t.data(), last, out, 10);
    return result.ec == std::errc() && result.ptr == last && !t.empty();
}

// Parse "start...step...end" or "start...end" (step defaults to 1).
// Returns empty vector if the value doesn't look like a range.
// Throws std::runtime_error if a part is not an integer or step is zero.
inline std::vector<std::string> parse_range_notation(const std::string& val) {
    size_t p1 = val.find("...");
    if (p1 == std::string::npos) return {};

    size_t p2 = val.find("...", p1 + 3);

    int start = 0;
    int step = 1;
    int end = 0;
    bool ok = parse_range_int(val.substr(0, p1), start);
    if (p2 == std::string::npos) {
        ok = ok && parse_range_int(val.substr(p1 + 3), end);
    } else {
        ok = ok && parse_range_int(val.substr(p1 + 3, p2 - p1 - 3), step) &&
             parse_range_int(val.substr(p2 + 3), end);
    }
    if (!ok)
        throw std::runtime_error("Invalid range '" + val +
                                 "' (expected integers start...end or start...step...end)");

    if (step == 0) throw std::runtime_error("Range step must not be zero");

    // Count first in 64-bit so huge or INT_MAX-adjacent ranges neither overflow nor allocate.
    const long long span = step > 0 ? static_cast<long long>(end) - start
                                    : static_cast<long long>(start) - end;
    const long long count = span < 0 ? 0 : span / std::llabs(static_cast<long long>(step)) + 1;
    constexpr long long kMaxRangeValues = 10000;
    if (count > kMaxRangeValues)
        throw std::runtime_error("Range '" + val + "' has " + std::to_string(count) +
                                 " values (max " + std::to_string(kMaxRangeValues) + ")");

    std::vector<std::string> result;
    result.reserve(static_cast<size_t>(count));
    for (long long i = 0; i < count; ++i)
        result.push_back(std::to_string(static_cast<long long>(start) + i * step));
    return result;
}

}  // namespace range_parser

