#pragma once
#include <string>
#include <vector>
#include <map>

struct SearchSpace {
    struct Gene {
        std::string description;      // e.g., "tsmean.days"
        std::vector<std::string> values;  // possible values
    };
    std::vector<Gene> genes;

    // Total number of combinations (product of gene sizes)
    size_t total_combinations() const;

    // Decode a gene vector (indices) into a concrete XML string
    std::string decode(const std::vector<int>& indices) const;
};

// Extract search space from XML template.
// Each expansion point (pipe, range, choice) becomes a Gene.
// decode(indices) reconstructs a concrete XML by substituting
// the selected value for each gene.
SearchSpace extract_search_space(const std::string& xml_template);
