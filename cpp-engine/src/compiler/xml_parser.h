#pragma once

#include <string>
#include <vector>
#include <map>

struct XmlElement {
    std::string tag;
    std::map<std::string, std::string> attribs;
    std::vector<XmlElement> children;  // for <choice>
};

// Parse XML string → list of top-level elements.
// Wraps in <alpha>...</alpha> if needed (multiple sibling tags).
// Throws std::runtime_error on parse failure.
std::vector<XmlElement> parse_xml(const std::string& xml);
