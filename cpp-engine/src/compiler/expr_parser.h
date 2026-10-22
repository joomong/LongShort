#pragma once

#include <string>
#include <memory>
#include <vector>

class Module;
struct DataPanel;

// Parse an expression string into a Module tree.
// All created modules are appended to `owned`; caller manages lifetime.
// Returns a non-owning pointer to the root (output) module.
// Allowed identifiers: fields registered in the DataPanel, plus derived returns
// Allowed operators: +, -, *, / and parentheses
// Functions (identifier followed by '(', whitespace allowed): abs(x) log(x) sqrt(x) sign(x) power(x, p) signedpower(x, p);
//   x is any expression, p a numeric literal with optional '-'. Each is Chain(x, MUnary<Op>), same Op as the
//   chain tag. Any other name followed by '(' is an error; a name not followed by '(' is a data field.
// Numeric literals produce MConstant modules.
// Special: "returns" is the panel field when it has one (thin-v1), else close / Delay(close, 1) - 1.0
// Throws std::runtime_error on invalid syntax or unknown identifier.
Module* parse_expr(const std::string& expr,
                   const DataPanel* panel,
                   std::vector<std::unique_ptr<Module>>& owned);
