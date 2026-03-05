#include "expansion.h"
#include "range_parser.h"
#include <algorithm>
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

    std::vector<std::map<std::string, std::string>> result = {{}};
    for (size_t ki = 0; ki < keys.size(); ++ki) {
        std::vector<std::map<std::string, std::string>> next;
        for (auto& existing : result) {
            for (auto& v : values[ki]) {
                auto copy = existing;
                copy[keys[ki]] = v;
                next.push_back(std::move(copy));
            }
        }
        result = std::move(next);
    }
    return result;
}

// ---------------------------------------------------------------------------
// Expand a single XmlElement into a list of (tag, attribs) Op lists.
// Each XmlElement expands into N ops (one per variant).
// For regular elements: cartesian product of attribute values.
// For <choice>: each child is an alternative (each expanded recursively).
// For <shuffle>: all permutations of children sequences.
// ---------------------------------------------------------------------------

static std::vector<Op> expand_element_single(const XmlElement& el);

// Returns a list of "op sequences" — each inner vector is one variant's
// contribution from this element (usually 1 op, but choice/shuffle may
// inject multiple ops if children themselves expand).
// Here we keep it simple: each alternative is exactly one Op.
static std::vector<Op> expand_element_single(const XmlElement& el) {
    // This function is not directly used — see expand_element_variants below.
    (void)el;
    return {};
}

// Returns a list of alternatives, each alternative being a list of Ops
// (for normal elements it's always a list of 1-element lists,
//  for choice/shuffle it may produce multi-op alternatives).
using AltList = std::vector<std::vector<Op>>;

static AltList expand_element(const XmlElement& el);

// Expand children of a <choice> or <shuffle> into AltLists per child.
static AltList expand_element(const XmlElement& el) {
    if (el.tag == "choice") {
        // Each child is one alternative.
        AltList result;
        for (auto& child : el.children) {
            AltList child_alts = expand_element(child);
            for (auto& alt : child_alts)
                result.push_back(alt);
        }
        return result;
    }

    if (el.tag == "shuffle") {
        // Collect one representative Op per child (first expansion).
        // Then generate all permutations of those ops.
        std::vector<Op> child_ops;
        for (auto& child : el.children) {
            AltList child_alts = expand_element(child);
            if (!child_alts.empty() && !child_alts[0].empty())
                child_ops.push_back(child_alts[0][0]);
        }
        // Sort to get canonical starting order for next_permutation.
        std::sort(child_ops.begin(), child_ops.end(),
            [](const Op& a, const Op& b){ return a.first < b.first; });

        AltList result;
        do {
            std::vector<Op> seq(child_ops.begin(), child_ops.end());
            result.push_back(seq);
        } while (std::next_permutation(child_ops.begin(), child_ops.end(),
            [](const Op& a, const Op& b){ return a.first < b.first; }));
        return result;
    }

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

    // First pass: expand each element into its alternatives.
    // For <choice bind="same">, we need to synchronise matching indices.
    // We handle bind="same" by collecting groups of bound choices and
    // enforcing they share the same child-index.

    // Identify bound choice groups.
    // key: bind label → list of element indices that share that label.
    // We use the "bind" attribute value as the group key.
    // Non-bound elements have bind="".

    struct ElemInfo {
        AltList alts;
        std::string bind_group; // "" means unbound
    };

    std::vector<ElemInfo> infos;
    infos.reserve(elements.size());

    for (auto& el : elements) {
        ElemInfo info;
        info.alts = expand_element(el);
        if (el.tag == "choice") {
            auto it = el.attribs.find("bind");
            if (it != el.attribs.end())
                info.bind_group = it->second;
        }
        infos.push_back(std::move(info));
    }

    // Collect bind groups: bind_label → list of indices in `infos`
    std::map<std::string, std::vector<size_t>> bind_groups;
    for (size_t i = 0; i < infos.size(); ++i) {
        if (!infos[i].bind_group.empty())
            bind_groups[infos[i].bind_group].push_back(i);
    }

    // For bound groups, all members must have the same number of alternatives.
    // We'll iterate the cartesian product but constrain bound indices to
    // use the same alternative index.

    // Build pipelines via cartesian product.
    // State: for each element index, current alternative index.
    // Constraints: for bound groups, all share the same index.

    // We enumerate using a recursive approach building a list of pipelines.
    std::vector<Pipeline> pipelines = {{}};  // start with one empty pipeline

    for (size_t ei = 0; ei < infos.size(); ++ei) {
        const AltList& alts = infos[ei].alts;
        const std::string& bg = infos[ei].bind_group;

        // Check if this index is already "driven" by a bind group whose
        // first member was already processed.
        bool already_driven = false;
        if (!bg.empty()) {
            auto& grp = bind_groups[bg];
            if (grp[0] != ei) {
                // This is a secondary member of the group.
                // The primary (grp[0]) already expanded pipelines;
                // we need to re-use those choices.
                // The bind tracking is embedded in the pipeline metadata below.
                already_driven = true;
            }
        }

        if (already_driven) {
            // The number of pipelines was already multiplied by the primary.
            // Each pipeline was tagged with a bind index; apply it here.
            // We stored the bind index by checking which alt of the primary
            // corresponds to each pipeline. Since primary created N copies
            // (one per alt), and this element has the same N alts, we apply
            // alt[i % alts.size()] for pipeline i within each primary group.
            //
            // Implementation: pipelines were expanded N times by the primary.
            // Pipelines are grouped in blocks of N (alts.size() of primary).
            // Pipeline i within its block corresponds to alt index i % N.
            size_t N = alts.size();
            std::vector<Pipeline> next;
            next.reserve(pipelines.size());
            for (size_t pi = 0; pi < pipelines.size(); ++pi) {
                // Determine which bind index this pipeline corresponds to.
                // We need to know the block size from the primary expansion.
                // The primary's index is bind_groups[bg][0].
                // At that point, pipelines were expanded by alts.size().
                // We track this by storing the alt index in pipeline metadata
                // would be complex; instead use a simpler approach:
                // The pipelines are in groups. Group size = product of
                // expansions from primary onward that belong to the same bind.
                // For simplicity: alt index = (pi / stride) % N, where stride
                // is the product of all expansions of elements between primary
                // and this one.
                // This is correct for a simple cartesian product pattern.

                // Compute stride = product of alts sizes of all elements
                // strictly between primary and ei (non-bound or different group).
                size_t primary_idx = bind_groups[bg][0];
                size_t stride = 1;
                for (size_t j = primary_idx + 1; j < ei; ++j) {
                    stride *= infos[j].alts.size();
                }
                size_t alt_idx = (pi / stride) % N;

                Pipeline p = pipelines[pi];
                for (auto& op : alts[alt_idx])
                    p.push_back(op);
                next.push_back(std::move(p));
            }
            pipelines = std::move(next);
        } else {
            // Normal expansion: cartesian product.
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
        }

        if (pipelines.size() > static_cast<size_t>(MAX_ALPHA_COUNT))
            throw std::runtime_error(
                "Expansion produced more than " + std::to_string(MAX_ALPHA_COUNT) +
                " variants");
    }

    return pipelines;
}
