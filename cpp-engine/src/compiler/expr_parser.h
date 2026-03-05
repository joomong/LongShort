#pragma once

#include <string>
#include <memory>
#include <vector>

class Module;
struct DataPanel;

// Parse an expression string into a Module tree.
// All created modules are appended to `owned`; caller manages lifetime.
// Returns a non-owning pointer to the root (output) module.
// Allowed identifiers: open, high, low, close, volume, returns
// Allowed operators: +, -, *, /
// Numeric literals produce MConstant modules.
// Special: "returns" expands to close / Delay(close, 1) - 1.0
// Throws std::runtime_error on invalid syntax or unknown identifier.
Module* parse_expr(const std::string& expr,
                   const DataPanel* panel,
                   std::vector<std::unique_ptr<Module>>& owned);
