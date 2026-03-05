#include "search_space.h"
#include "../compiler/xml_parser.h"
#include "../compiler/expansion.h"

#include <sstream>
#include <stdexcept>
#include <regex>

// ---------------------------------------------------------------------------
// total_combinations
// ---------------------------------------------------------------------------
size_t SearchSpace::total_combinations() const {
    if (genes.empty()) return 1;
    size_t total = 1;
    for (const auto& g : genes) {
        total *= g.values.size();
    }
    return total;
}

// ---------------------------------------------------------------------------
// decode
// Rebuild XML by substituting gene values into the template.
std::string SearchSpace::decode(const std::vector<int>& indices) const {
    if (template_xml.empty()) {
        // No template stored — return empty
        return "";
    }

    if (indices.size() != genes.size()) {
        throw std::invalid_argument("decode: indices size mismatch");
    }

    std::string result = template_xml;
    for (size_t i = 0; i < genes.size(); ++i) {
        std::string placeholder = "__GENE_" + std::to_string(i) + "__";
        const std::string& val = genes[i].values[indices[i]];
        size_t pos = 0;
        while ((pos = result.find(placeholder, pos)) != std::string::npos) {
            result.replace(pos, placeholder.size(), val);
            pos += val.size();
        }
    }
    return result;
}

// ---------------------------------------------------------------------------
// extract_search_space
//
// Strategy: scan attribute values for pipe-separated lists and range syntax.
// For each expansion dimension found, record its possible values and replace
// the original value in the template with a __GENE_N__ placeholder.
//
// We do NOT use expand_pipelines() because that explodes all combinations;
// instead we enumerate dimensions independently.
//
// Supported patterns:
//   pipe:   "10|20|30"           → Gene with values ["10","20","30"]
//   range:  "20...10...60"       → Gene with values ["20","30","40","50","60"]
//           "20...60"            → Gene with values ["20","21",...,"60"]
//   (choice/shuffle: not yet supported; treated as literal)
// ---------------------------------------------------------------------------

namespace {

// Split string by delimiter, return parts
std::vector<std::string> split(const std::string& s, char delim) {
    std::vector<std::string> parts;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, delim)) {
        parts.push_back(item);
    }
    return parts;
}

// Try to parse range syntax "start...step...end" or "start...end"
// Returns empty vector if not a range pattern
std::vector<std::string> try_parse_range(const std::string& val) {
    // Must contain "..."
    if (val.find("...") == std::string::npos) return {};

    auto parts = split(val, '.');
    // Remove empty parts (from consecutive dots)
    std::vector<std::string> tokens;
    for (auto& p : parts) {
        if (!p.empty()) tokens.push_back(p);
    }

    // tokens should be 2 (start, end) or 3 (start, step, end)
    if (tokens.size() == 2) {
        int start = std::stoi(tokens[0]);
        int end   = std::stoi(tokens[1]);
        int step  = (start <= end) ? 1 : -1;
        std::vector<std::string> values;
        for (int v = start; step > 0 ? v <= end : v >= end; v += step) {
            values.push_back(std::to_string(v));
        }
        return values;
    } else if (tokens.size() == 3) {
        int start = std::stoi(tokens[0]);
        int step  = std::stoi(tokens[1]);
        int end   = std::stoi(tokens[2]);
        if (step == 0) return {};
        std::vector<std::string> values;
        for (int v = start; step > 0 ? v <= end : v >= end; v += step) {
            values.push_back(std::to_string(v));
        }
        return values;
    }
    return {};
}

// Check if a value looks like a pipe list (contains '|')
bool is_pipe(const std::string& val) {
    return val.find('|') != std::string::npos;
}

// Rebuild XML from XmlElements into a string (simplified, attribute order stable via map)
std::string element_to_xml(const XmlElement& elem) {
    std::string s = "<" + elem.tag;
    for (const auto& kv : elem.attribs) {
        s += " " + kv.first + "=\"" + kv.second + "\"";
    }
    if (elem.children.empty()) {
        s += "/>";
    } else {
        s += ">";
        for (const auto& child : elem.children) {
            s += element_to_xml(child);
        }
        s += "</" + elem.tag + ">";
    }
    return s;
}

} // namespace

SearchSpace extract_search_space(const std::string& xml_template) {
    SearchSpace space;

    // Parse the XML
    std::vector<XmlElement> elements = parse_xml(xml_template);

    // We will build a modified copy of elements with placeholders,
    // tracking genes as we go.
    int gene_idx = 0;

    // Process each element's attributes
    std::vector<XmlElement> modified = elements;
    for (auto& elem : modified) {
        // Skip structural tags (choice, shuffle)
        if (elem.tag == "choice" || elem.tag == "shuffle") continue;

        for (auto& kv : elem.attribs) {
            const std::string& val = kv.second;

            // Try range first
            auto range_vals = try_parse_range(val);
            if (!range_vals.empty()) {
                SearchSpace::Gene gene;
                gene.description = elem.tag + "." + kv.first;
                gene.values = std::move(range_vals);
                space.genes.push_back(std::move(gene));

                kv.second = "__GENE_" + std::to_string(gene_idx) + "__";
                ++gene_idx;
                continue;
            }

            // Try pipe
            if (is_pipe(val)) {
                auto pipe_vals = split(val, '|');
                SearchSpace::Gene gene;
                gene.description = elem.tag + "." + kv.first;
                gene.values = std::move(pipe_vals);
                space.genes.push_back(std::move(gene));

                kv.second = "__GENE_" + std::to_string(gene_idx) + "__";
                ++gene_idx;
                continue;
            }
        }
    }

    // Reconstruct the placeholder template string
    std::string tmpl;
    for (const auto& elem : modified) {
        tmpl += element_to_xml(elem);
    }

    space.template_xml = std::move(tmpl);

    return space;
}
