#pragma once

#include "module.h"
#include <charconv>
#include <cmath>
#include <cstdint>
#include <functional>
#include <initializer_list>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <tuple>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

struct DataPanel;

struct CompileError : public std::runtime_error {
    using std::runtime_error::runtime_error;
};

using ModuleFactory = std::function<std::unique_ptr<Module>(
    const std::map<std::string, std::string>& attribs,
    const DataPanel* panel)>;

class ModuleRegistry {
    std::unordered_map<std::string, ModuleFactory> factories_;
    std::vector<std::function<void(Module&, const std::vector<uint8_t>*)>> universe_binders_;
public:
    // Duplicate tags are errors; discovered modules cannot replace built-ins.
    void register_module(const std::string& tag, ModuleFactory factory);
    std::unique_ptr<Module> create(const std::string& tag,
                                   const std::map<std::string, std::string>& attribs,
                                   const DataPanel* panel) const;
    void add_universe_binder(std::function<void(Module&, const std::vector<uint8_t>*)> binder) {
        universe_binders_.push_back(std::move(binder));
    }
    void bind_universe(Module& module, const std::vector<uint8_t>* universe) const {
        for (const auto& bind : universe_binders_) bind(module, universe);
    }
    static ModuleRegistry& default_registry();
};

// Generated from the configured modules directory during the build.
void register_discovered_modules(ModuleRegistry& registry);

// Shared group= validation (sector|industry, trimmed, panel column present) for
// built-in group tags and discovered GroupParameter fields. Returns the name.
std::string validate_group_attribute(const std::map<std::string, std::string>& attribs,
                                     const std::string& tag, const DataPanel* panel,
                                     const std::string& key = "group");

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

namespace module_registration_detail {
template<class T, class = void> struct HasPanel : std::false_type {};
template<class T> struct HasPanel<T, std::void_t<decltype(&T::panel)>> : std::true_type {};
template<class T, class = void> struct HasUniverse : std::false_type {};
template<class T> struct HasUniverse<T, std::void_t<decltype(&T::universe)>> : std::true_type {};
}

// C++ owns module construction, parameter types and defaults. The builder is used
// only during registry setup, before any XML is compiled; no static initialization.
template<class T>
class ModuleRegistration {
    using Setter = std::function<void(T&, const std::string&, const DataPanel*)>;
    struct Definition {
        std::string tag;
        std::map<std::string, Setter> parameters;
    };
    std::shared_ptr<Definition> definition_;
public:
    ModuleRegistration(ModuleRegistry& registry, std::string tag)
        : definition_(std::make_shared<Definition>()) {
        static_assert(std::is_base_of_v<Module, T> && !std::is_abstract_v<T>,
                      "Registered modules must be concrete Module types");
        for (const char* reserved : {"data", "signal", "event", "choice", "alpha", "meta",
                                     "ew", "mvo", "minvar", "erc", "ivol", "spca", "glasso"})
            if (tag == reserved) throw CompileError("Reserved module tag: '" + tag + "'");
        if (tag.empty()) throw CompileError("Module tag must not be empty");
        definition_->tag = std::move(tag);
        registry.register_module(definition_->tag, [definition = definition_](const auto& attrs, const DataPanel* panel) {
            auto module = std::make_unique<T>();
            if constexpr (module_registration_detail::HasPanel<T>::value) {
                static_assert(std::is_same_v<decltype(&T::panel), const DataPanel* T::*>,
                              "panel must be a public const DataPanel* instance member");
                module->panel = panel;
            }
            for (const auto& [key, value] : attrs) {
                if (key == "input") continue; // resolved by the pipeline compiler
                const auto it = definition->parameters.find(key);
                if (it == definition->parameters.end())
                    throw CompileError("Unknown attribute '" + key + "' for <" + definition->tag + ">");
                it->second(*module, value, panel);
            }
            return module;
        });
        if constexpr (module_registration_detail::HasUniverse<T>::value) {
            static_assert(std::is_same_v<decltype(&T::universe), const std::vector<uint8_t>* T::*>,
                          "universe must be a public const std::vector<uint8_t>* instance member");
            registry.add_universe_binder([](Module& module, const std::vector<uint8_t>* universe) {
                if (auto* typed = dynamic_cast<T*>(&module)) typed->universe = universe;
            });
        }
    }

    template<class P>
    ModuleRegistration& parameter(std::string name, P T::*member) {
        static_assert(std::is_same_v<P, IntParameter> || std::is_same_v<P, FloatParameter> ||
                      std::is_same_v<P, GroupParameter>, "XML parameters must use SDK parameter types");
        if (name.empty() || name == "input")
            throw CompileError("Invalid parameter name '" + name + "' for <" + definition_->tag + ">");
        const auto tag = definition_->tag;
        const auto key = name;
        const bool added = definition_->parameters.emplace(std::move(name),
            [member, tag, key](T& module, const std::string& value, const DataPanel* panel) {
                if constexpr (std::is_same_v<P, IntParameter>)
                    module.*member = IntParameter(module_attributes::parse_int(value, tag, key));
                else if constexpr (std::is_same_v<P, FloatParameter>)
                    module.*member = FloatParameter(module_attributes::parse_float(value, tag, key));
                else
                    module.*member = GroupParameter(validate_group_attribute({{key, value}}, tag, panel, key));
            }).second;
        if (!added) throw CompileError("Duplicate parameter '" + key + "' for <" + tag + ">");
        return *this;
    }
};

template<class T>
ModuleRegistration<T> register_type(ModuleRegistry& registry, std::string tag) {
    return ModuleRegistration<T>(registry, std::move(tag));
}

// Name matches the header basename; Type may be namespace-qualified. Empty
// parameter lists are allowed. All registration runs explicitly at registry setup.
#define MODULE_PARAMETER(Type, Member) std::make_pair(#Member, &Type::Member)
#define REGISTER_MODULE(Name, Type, Tag, ...) \
    inline void register_##Name(ModuleRegistry& registry) { \
        std::apply([&](auto... parameters) { \
            auto registration = register_type<Type>(registry, Tag); \
            (registration.parameter(parameters.first, parameters.second), ...); \
        }, std::make_tuple(__VA_ARGS__)); \
    }
