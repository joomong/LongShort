#include "search_space.h"
#include "../compiler/xml_parser.h"
#include "../compiler/expansion.h"
#include "../compiler/range_parser.h"

#include <sstream>
#include <stdexcept>
#include <regex>
#include <limits>
#include <algorithm>
#include <unordered_set>

// ---------------------------------------------------------------------------
// total_combinations
// ---------------------------------------------------------------------------
size_t SearchSpace::total_combinations() const {
    if (genes.empty()) return 1;
    size_t total = 1;
    for (const auto& g : genes) {
        if (g.values.empty()) return 0;
        if (total > std::numeric_limits<size_t>::max() / g.values.size())
            return std::numeric_limits<size_t>::max();
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
        item = range_parser::trim_copy(item);
        if (std::find(parts.begin(), parts.end(), item) == parts.end()) parts.push_back(item);
    }
    return parts;
}

// Try to parse range syntax "start...step...end" or "start...end".
// Returns empty vector if not a range pattern.
std::vector<std::string> try_parse_range(const std::string& val) {
    return range_parser::parse_range_notation(val);
}

// Check if a value looks like a pipe list (contains '|')
bool is_pipe(const std::string& val) {
    return val.find('|') != std::string::npos;
}

std::string escape_attribute(const std::string& value) {
    std::string escaped;
    for (char c : value) {
        if (c == '&') escaped += "&amp;";
        else if (c == '<') escaped += "&lt;";
        else if (c == '>') escaped += "&gt;";
        else if (c == '"') escaped += "&quot;";
        else escaped += c;
    }
    return escaped;
}

// Attribute order is stable and XML expressions retain escaping.
std::string element_to_xml(const XmlElement& elem) {
    std::string s = "<" + elem.tag;
    for (const auto& kv : elem.attribs) {
        s += " " + kv.first + "=\"" + escape_attribute(kv.second) + "\"";
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

    // Structural choices use the compiler's existing expansion contract,
    // including bound choices and shuffle. Ordinary parameter searches stay
    // factored so large Cartesian spaces are never expanded up front.
    if (std::any_of(elements.begin(), elements.end(), [](const XmlElement& element) {
            return element.tag == "choice" || element.tag == "shuffle";
        })) {
        SearchSpace::Gene gene;
        gene.description = "pipeline";
        std::unordered_set<std::string> seen;
        for (const auto& pipeline : expand_pipelines(elements)) {
            std::string xml;
            for (const auto& op : pipeline) {
                XmlElement element;
                element.tag = op.first;
                element.attribs = op.second;
                if (!xml.empty()) xml += "\n";
                xml += element_to_xml(element);
            }
            if (seen.insert(xml).second) gene.values.push_back(std::move(xml));
        }
        if (gene.values.empty()) throw std::invalid_argument("XML has no candidate pipelines");
        space.genes.push_back(std::move(gene));
        space.template_xml = "__GENE_0__";
        return space;
    }

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
                for (auto& value : pipe_vals) value = escape_attribute(value);
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

    // Reconstruct the placeholder template string, one module per line like
    // the compiler's expanded XML.
    std::string tmpl;
    for (const auto& elem : modified) {
        if (!tmpl.empty()) tmpl += "\n";
        tmpl += element_to_xml(elem);
    }

    space.template_xml = std::move(tmpl);

    return space;
}
