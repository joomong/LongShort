#pragma once

#include "xml_parser.h"
#include <stdexcept>
#include <vector>
#include <map>
#include <string>

using Op = std::pair<std::string, std::map<std::string, std::string>>;
using Pipeline = std::vector<Op>;

// Hard cap to prevent unbounded combinatorial explosion during XML expansion.
static constexpr int MAX_ALPHA_COUNT = 5000;

// Expand elements into variant pipelines.
// - Pipe:  "A|B|C" in any attribute value → that many variants
// - Range: "20...10...60" or "20...60" → list of string ints
// - <choice>: children are alternatives; bind="same" synchronises across occurrences
// - <shuffle>: generates all permutations of children
// Throws std::runtime_error if variant count exceeds MAX_ALPHA_COUNT.
std::vector<Pipeline> expand_pipelines(const std::vector<XmlElement>& elements);
