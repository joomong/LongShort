#pragma once

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

// Parse "start...step...end" or "start...end" (step defaults to 1).
// Returns empty vector if the value doesn't look like a range.
// Throws std::runtime_error if step is zero.
inline std::vector<std::string> parse_range_notation(const std::string& val) {
    size_t p1 = val.find("...");
    if (p1 == std::string::npos) return {};

    size_t p2 = val.find("...", p1 + 3);

    int start = 0;
    int step = 0;
    int end = 0;
    if (p2 == std::string::npos) {
        start = std::stoi(trim_copy(val.substr(0, p1)));
        step = 1;
        end = std::stoi(trim_copy(val.substr(p1 + 3)));
    } else {
        start = std::stoi(trim_copy(val.substr(0, p1)));
        step = std::stoi(trim_copy(val.substr(p1 + 3, p2 - p1 - 3)));
        end = std::stoi(trim_copy(val.substr(p2 + 3)));
    }

    if (step == 0) throw std::runtime_error("Range step must not be zero");

    std::vector<std::string> result;
    if (step > 0) {
        for (int v = start; v <= end; v += step)
            result.push_back(std::to_string(v));
    } else {
        for (int v = start; v >= end; v += step)
            result.push_back(std::to_string(v));
    }
    return result;
}

}  // namespace range_parser

