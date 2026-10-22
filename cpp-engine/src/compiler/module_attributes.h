#pragma once

#include "dag_builder.h"
#include <charconv>
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

// Plain decimal only: -?digits[.digits] (".5" and "5." included). from_chars with
// chars_format::fixed rejects exponents, hex floats and a leading '+'; the text
// must be consumed fully. The finite check is the single place inf/nan are rejected.
// The text is rounded once, directly to float (no decimal->double->float double
// rounding); a decimal outside float range is out_of_range and rejected.
inline bool parse_decimal(const std::string& raw, float& out) {
    const std::string value = detail::trim(raw);
    const char* first = value.data();
    const char* last = first + value.size();
    float parsed = 0;
    const auto result = std::from_chars(first, last, parsed, std::chars_format::fixed);
    if (result.ec != std::errc() || result.ptr != last || !std::isfinite(parsed))
        return false;
    out = parsed;
    return true;
}

inline int parse_int(const std::string& raw,
                     const std::string& tag,
                     const std::string& key) {
    const std::string value = detail::trim(raw);
    const char* last = value.data() + value.size();
    int parsed = 0;
    const auto result = std::from_chars(value.data(), last, parsed, 10);
    if (result.ec == std::errc() && result.ptr == last && !value.empty())
        return parsed;
    throw detail::invalid_value(raw, tag, key, "integer within int range");
}

// Every numeric XML attribute (FloatParameter, combination shrink/alpha).
inline float parse_float(const std::string& raw,
                         const std::string& tag,
                         const std::string& key) {
    float parsed = 0;
    if (parse_decimal(raw, parsed))
        return parsed;
    throw detail::invalid_value(raw, tag, key, "decimal number");
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
