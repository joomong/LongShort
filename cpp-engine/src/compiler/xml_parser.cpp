#include "xml_parser.h"
#include <pugixml.hpp>
#include <stdexcept>
#include <sstream>

static XmlElement node_to_element(const pugi::xml_node& node) {
    XmlElement el;
    el.tag = node.name();
    for (auto& attr : node.attributes())
        el.attribs[attr.name()] = attr.value();
    for (auto& child : node.children()) {
        if (child.type() == pugi::node_element)
            el.children.push_back(node_to_element(child));
    }
    return el;
}

std::vector<XmlElement> parse_xml(const std::string& xml) {
    // Wrap in a root element so multiple sibling tags parse correctly.
    std::string wrapped = "<alpha>" + xml + "</alpha>";

    pugi::xml_document doc;
    pugi::xml_parse_result result = doc.load_string(wrapped.c_str());
    if (!result)
        throw std::runtime_error(std::string("XML parse error: ") + result.description());

    pugi::xml_node root = doc.child("alpha");
    if (!root)
        throw std::runtime_error("XML parse error: empty document");

    std::vector<XmlElement> elements;
    for (auto& child : root.children()) {
        if (child.type() == pugi::node_element)
            elements.push_back(node_to_element(child));
    }
    return elements;
}
