#pragma once

#include "dag_builder.h"
#include <cmath>
#include <initializer_list>
#include <map>
#include <string>

// Strict attribute parsing shared by built-in module factories, the structural
// tags handled in compile_pipeline (data, signal, event) and automatically
// discovered modules: unknown keys and malformed values are compile errors.
namespace module_attributes {
namespace detail {
inline std::string trim(const std::string& raw) {
    const auto first = raw.find_first_not_of(" \t\n\r\f\v");
    if (first == std::string::npos)
        return {};
    const auto last = raw.find_last_not_of(" \t\n\r\f\v");
    return raw.substr(first, last - first + 1);
}

inline CompileError invalid_value(const std::string& raw,
                                  const std::string& tag,
                                  const std::string& key,
                                  const char* expected) {
    return CompileError("Invalid value for <" + tag + "> attribute '" + key +
                        "': '" + raw + "' (expected " + expected + ")");
}
}  // namespace detail

inline int parse_int(const std::string& raw,
                     const std::string& tag,
                     const std::string& key) {
    const std::string value = detail::trim(raw);
    try {
        std::size_t consumed = 0;
        const int parsed = std::stoi(value, &consumed, 10);
        if (consumed == value.size())
            return parsed;
    } catch (const std::invalid_argument&) {
    } catch (const std::out_of_range&) {
    }
    throw detail::invalid_value(raw, tag, key, "integer within int range");
}

inline double parse_float(const std::string& raw,
                          const std::string& tag,
                          const std::string& key) {
    const std::string value = detail::trim(raw);
    try {
        std::size_t consumed = 0;
        const double parsed = std::stod(value, &consumed);
        if (consumed == value.size() && std::isfinite(parsed))
            return parsed;
    } catch (const std::invalid_argument&) {
    } catch (const std::out_of_range&) {
    }
    throw detail::invalid_value(raw, tag, key, "finite number");
}

inline bool parse_bool(const std::string& raw,
                       const std::string& tag,
                       const std::string& key) {
    const std::string value = detail::trim(raw);
    if (value == "true" || value == "1")
        return true;
    if (value == "false" || value == "0")
        return false;
    throw detail::invalid_value(raw, tag, key, "true|false|1|0");
}

inline void validate_keys(const std::map<std::string, std::string>& attrs,
                          std::initializer_list<const char*> keys,
                          const std::string& tag,
                          bool allow_input = true) {
    for (const auto& attr : attrs) {
        const std::string& name = attr.first;
        // The compiler resolves input= before passing attrs to a registry
        // module; structural tags (data, signal, event, choice) never read it.
        if (allow_input && name == "input")
            continue;
        bool known = false;
        for (const char* key : keys) {
            if (name == key) {
                known = true;
                break;
            }
        }
        if (!known)
            throw CompileError("Unknown attribute '" + name + "' for <" + tag + ">");
    }
}
}  // namespace module_attributes
