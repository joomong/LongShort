#pragma once

#include "dag_builder.h"
#include <cmath>
#include <initializer_list>
#include <map>
#include <string>

// Attribute parsing for automatically discovered modules. Built-in module
// factories keep their existing defaults and validation rules.
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

inline void validate_keys(const std::map<std::string, std::string>& attrs,
                          std::initializer_list<const char*> keys,
                          const std::string& tag) {
    for (const auto& attr : attrs) {
        const std::string& name = attr.first;
        // The compiler resolves input= before passing attrs to the factory.
        if (name == "input")
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
