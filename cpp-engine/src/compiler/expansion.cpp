#include "expansion.h"
#include "range_parser.h"
#include "module_attributes.h"
#include <algorithm>
#include <functional>
#include <sstream>
#include <stdexcept>

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Split on '|', trim each piece.
static std::vector<std::string> split_pipe(const std::string& val) {
    std::vector<std::string> parts;
    std::istringstream ss(val);
    std::string tok;
    while (std::getline(ss, tok, '|'))
        parts.push_back(range_parser::trim_copy(tok));
    return parts;
}

// Expand a single attribute value into multiple string values.
// Checks range first (has "..."), then pipe (has "|"), else single value.
static std::vector<std::string> expand_value(const std::string& val) {
    if (val.find("...") != std::string::npos) {
        auto r = range_parser::parse_range_notation(val);
        if (!r.empty()) return r;
    }
    if (val.find('|') != std::string::npos) {
        return split_pipe(val);
    }
    return {val};
}

// ---------------------------------------------------------------------------
// Cartesian product over a map of attribute value lists.
// Returns a list of attrib maps, one per combination.
// ---------------------------------------------------------------------------

static std::vector<std::map<std::string, std::string>> cartesian_attribs(
    const std::map<std::string, std::string>& attribs)
{
    // Build per-key expanded values
    std::vector<std::string> keys;
    std::vector<std::vector<std::string>> values;
    for (auto& kv : attribs) {
        keys.push_back(kv.first);
        values.push_back(expand_value(kv.second));
    }

    size_t total = 1;
    for (const auto& vs : values)
        total *= std::max<size_t>(1, vs.size());

    std::vector<std::map<std::string, std::string>> result;
    result.reserve(total);
    std::map<std::string, std::string> current;

    std::function<void(size_t)> dfs = [&](size_t ki) {
        if (ki == keys.size()) {
            result.push_back(current);
            return;
        }
        for (const auto& v : values[ki]) {
            current[keys[ki]] = v;
            dfs(ki + 1);
        }
    };

    if (keys.empty()) {
        result.push_back({});
    } else {
        dfs(0);
    }

    return result;
}

// ---------------------------------------------------------------------------
// Expand a single XmlElement into a list of (tag, attribs) Op lists.
// Each XmlElement expands into N ops (one per variant).
// For regular elements: cartesian product of attribute values.
// For <choice>: each child is an alternative (each expanded recursively).
// ---------------------------------------------------------------------------

// Returns a list of alternatives, each alternative being a list of Ops
// (for normal elements it's always a list of 1-element lists,
//  for choice it may produce multi-op alternatives).
using AltList = std::vector<std::vector<Op>>;

static AltList expand_element(const XmlElement& el);

static AltList expand_element(const XmlElement& el) {
    if (el.tag == "choice") {
        module_attributes::validate_keys(el.attribs, {}, "choice", false);
        // Each child is one alternative.
        AltList result;
        for (auto& child : el.children) {
            AltList child_alts = expand_element(child);
            for (auto& alt : child_alts)
                result.push_back(alt);
        }
        return result;
    }

    // Only <choice> reads children; e.g. <data><delay/></data> would otherwise
    // compile silently without the delay (same-bar look-ahead).
    if (!el.children.empty())
        throw CompileError("<" + el.tag + "> does not take child elements");

    // Normal element: cartesian product of attribute expansions.
    auto attrib_combos = cartesian_attribs(el.attribs);
    AltList result;
    for (auto& combo : attrib_combos) {
        result.push_back({ {el.tag, combo} });
    }
    return result;
}

// ---------------------------------------------------------------------------
// Main expansion entry point
// ---------------------------------------------------------------------------

std::vector<Pipeline> expand_pipelines(const std::vector<XmlElement>& elements) {
    // Each element in the pipeline produces an AltList.
    // We need the cartesian product across all positions.

    std::vector<Pipeline> pipelines = {{}};  // start with one empty pipeline

    for (const auto& el : elements) {
        const AltList alts = expand_element(el);
        std::vector<Pipeline> next;
        next.reserve(pipelines.size() * alts.size());
        for (auto& base : pipelines) {
            for (auto& alt : alts) {
                Pipeline p = base;
                for (auto& op : alt)
                    p.push_back(op);
                next.push_back(std::move(p));
            }
        }
        pipelines = std::move(next);

        if (pipelines.size() > static_cast<size_t>(MAX_ALPHA_COUNT))
            throw std::runtime_error(
                "Expansion produced more than " + std::to_string(MAX_ALPHA_COUNT) +
                " variants");
    }

    return pipelines;
}
