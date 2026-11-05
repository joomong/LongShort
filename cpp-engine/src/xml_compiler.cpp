#include "xml_compiler.h"
#include <pugixml.hpp>
#include <stdexcept>
#include <sstream>
#include <algorithm>
#include <functional>
#include "modules/Data.h"
#include "modules/Delay.h"
#include "modules/Ops.h"
#include "modules/Constant.h"
#include "modules/Chain.h"
#include "modules/Unary.h"
#include <cctype>
#include <cmath>
#include <limits>
#include <vector>
#include <memory>
#include <charconv>
#include "modules/MetaSource.h"
#include "modules/TsMean.h"
#include "modules/TsRank.h"
#include "modules/TsZscore.h"
#include "modules/Decay.h"
#include "modules/Event.h"
#include "modules/Rank.h"
#include "modules/Zscore.h"
#include "modules/Neutralize.h"
#include "modules/SignalCache.h"
#include "modules/TsEwma.h"
#include "modules/Truncate.h"
#include "modules/TsDelta.h"
#include "modules/TsSum.h"
#include "modules/TsStdDev.h"
#include "modules/TsArgExtreme.h"
#include "modules/TsFill.h"
#include "modules/Winsorize.h"
#include "modules/Normalize.h"
#include "modules/Hump.h"
#include "modules/GroupOps.h"
#include "modules/RankSharp.h"
#include "modules/Ffd.h"
#include "modules/Affine.h"
#include "modules/PcaResid.h"
#include <cstdlib>
#include <string>

namespace range_parser {

std::string trim_copy(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

// Strict integer: -?digits, consumed fully, within int range.
static bool parse_range_int(const std::string& text, int& out) {
    const std::string t = trim_copy(text);
    const char* last = t.data() + t.size();
    const auto result = std::from_chars(t.data(), last, out, 10);
    return result.ec == std::errc() && result.ptr == last && !t.empty();
}

// Parse "start...step...end" or "start...end" (step defaults to 1).
// Returns empty vector if the value doesn't look like a range.
// Throws std::runtime_error if a part is not an integer or step is zero.
std::vector<std::string> parse_range_notation(const std::string& val) {
    size_t p1 = val.find("...");
    if (p1 == std::string::npos) return {};

    size_t p2 = val.find("...", p1 + 3);

    int start = 0;
    int step = 1;
    int end = 0;
    bool ok = parse_range_int(val.substr(0, p1), start);
    if (p2 == std::string::npos) {
        ok = ok && parse_range_int(val.substr(p1 + 3), end);
    } else {
        ok = ok && parse_range_int(val.substr(p1 + 3, p2 - p1 - 3), step) &&
             parse_range_int(val.substr(p2 + 3), end);
    }
    if (!ok)
        throw std::runtime_error("Invalid range '" + val +
                                 "' (expected integers start...end or start...step...end)");

    if (step == 0) throw std::runtime_error("Range step must not be zero");

    // Count first in 64-bit so huge or INT_MAX-adjacent ranges neither overflow nor allocate.
    const long long span = step > 0 ? static_cast<long long>(end) - start
                                    : static_cast<long long>(start) - end;
    const long long count = span < 0 ? 0 : span / std::llabs(static_cast<long long>(step)) + 1;
    constexpr long long kMaxRangeValues = 10000;
    if (count > kMaxRangeValues)
        throw std::runtime_error("Range '" + val + "' has " + std::to_string(count) +
                                 " values (max " + std::to_string(kMaxRangeValues) + ")");

    std::vector<std::string> result;
    result.reserve(static_cast<size_t>(count));
    for (long long i = 0; i < count; ++i)
        result.push_back(std::to_string(static_cast<long long>(start) + i * step));
    return result;
}

}  // namespace range_parser

std::string format_xml(const std::string& xml) {
    pugi::xml_document doc;
    const auto result = doc.load_string(xml.c_str(), pugi::parse_full | pugi::parse_fragment);
    if (!result)
        throw std::runtime_error(std::string("XML parse error: ") + result.description());
    std::ostringstream out;
    doc.print(out, "  ");
    return out.str();
}

std::string pipeline_xml(const std::string& xml) {
    pugi::xml_document doc;
    const auto result = doc.load_string(xml.c_str(), pugi::parse_full | pugi::parse_fragment);
    if (!result) throw std::runtime_error(std::string("XML parse error: ") + result.description());
    auto root = doc.child("alpha");
    if (!root) return xml;
    unsigned roots = 0;
    for (auto node : doc.children()) if (node.type() == pugi::node_element) ++roots;
    if (roots != 1 || root.first_attribute())
        throw std::runtime_error("Expected one <alpha> root without attributes");
    unsigned pipelines = 0, metadata = 0;
    for (auto node : root.children()) {
        if (node.type() != pugi::node_element) continue;
        const std::string name = node.name();
        if (name == "pipeline") ++pipelines;
        else if (name == "metadata") ++metadata;
        else throw std::runtime_error("Unexpected <alpha> child: " + name);
    }
    if (pipelines != 1 || metadata > 1 || root.child("pipeline").first_attribute())
        throw std::runtime_error("Expected one <pipeline> and at most one <metadata>");
    std::ostringstream out;
    for (auto node : root.child("pipeline").children()) {
        if (node.type() == pugi::node_pcdata) {
            // Envelope indentation must not accumulate on editor text notes.
            const auto text = range_parser::trim_copy(node.value());
            if (!text.empty()) {
                node.set_value(text.c_str());
                node.print(out, "  ");
                out << '\n';
            }
        } else node.print(out, "  ");
    }
    return out.str();
}

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
    std::string wrapped = "<alpha>" + pipeline_xml(xml) + "</alpha>";

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

// ---------------------------------------------------------------------------
// Tokeniser
// ---------------------------------------------------------------------------

namespace {
enum class TokKind { Ident, Number, Plus, Minus, Star, Slash, LParen, RParen, Comma, Eof };

struct Token {
    TokKind kind;
    std::string text;
};

static std::vector<Token> tokenise(const std::string& src) {
    std::vector<Token> toks;
    size_t i = 0;
    while (i < src.size()) {
        if (std::isspace((unsigned char)src[i])) { ++i; continue; }
        char c = src[i];
        if (c == '+') { toks.push_back({TokKind::Plus,   "+"}); ++i; continue; }
        if (c == '-') { toks.push_back({TokKind::Minus,  "-"}); ++i; continue; }
        if (c == '*') { toks.push_back({TokKind::Star,   "*"}); ++i; continue; }
        if (c == '/') { toks.push_back({TokKind::Slash,  "/"}); ++i; continue; }
        if (c == '(') { toks.push_back({TokKind::LParen, "("}); ++i; continue; }
        if (c == ')') { toks.push_back({TokKind::RParen, ")"}); ++i; continue; }
        if (c == ',') { toks.push_back({TokKind::Comma,  ","}); ++i; continue; }
        if (std::isdigit((unsigned char)c) || c == '.') {
            size_t start = i;
            while (i < src.size() && (std::isdigit((unsigned char)src[i]) || src[i] == '.'))
                ++i;
            if (i < src.size() && (src[i] == 'e' || src[i] == 'E')) {
                size_t j = i + 1;
                if (j < src.size() && (src[j] == '+' || src[j] == '-')) ++j;
                if (j < src.size() && std::isdigit((unsigned char)src[j])) {
                    while (j < src.size() && std::isdigit((unsigned char)src[j])) ++j;
                    throw std::runtime_error("expr_parser: scientific notation is not allowed ('" +
                                             src.substr(start, j - start) + "'); write it as a plain decimal");
                }
            }
            toks.push_back({TokKind::Number, src.substr(start, i - start)});
            continue;
        }
        if (std::isalpha((unsigned char)c) || c == '_') {
            size_t start = i;
            while (i < src.size() && (std::isalnum((unsigned char)src[i]) || src[i] == '_'))
                ++i;
            // A wide dataset field is `<dataset>.<field>`: one dot, no whitespace, and a field
            // part of [A-Za-z0-9_]+ (it may start with a digit: AFL.10DMACD).
            if (i < src.size() && src[i] == '.') {
                size_t j = i + 1;
                while (j < src.size() && (std::isalnum((unsigned char)src[j]) || src[j] == '_'))
                    ++j;
                if (j == i + 1)
                    throw std::runtime_error("expr_parser: expected a field name after '" +
                                             src.substr(start, i + 1 - start) + "'");
                if (j < src.size() && src[j] == '.')
                    throw std::runtime_error("expr_parser: a data name has exactly one dot ('" +
                                             src.substr(start, j + 1 - start) + "')");
                i = j;
            }
            toks.push_back({TokKind::Ident, src.substr(start, i - start)});
            continue;
        }
        throw std::runtime_error(std::string("expr_parser: unexpected character '") + c + "'");
    }
    toks.push_back({TokKind::Eof, ""});
    return toks;
}

// ---------------------------------------------------------------------------
// Recursive descent parser
// ---------------------------------------------------------------------------

struct Parser {
    const std::vector<Token>& toks;
    size_t pos;
    const DataPanel* panel;
    std::vector<std::unique_ptr<Module>>& owned;

    const Token& cur() const { return toks[pos]; }
    void consume() { ++pos; }

    void expect(TokKind k, const char* msg) {
        if (toks[pos].kind != k)
            throw std::runtime_error(std::string("expr_parser: expected ") + msg);
        ++pos;
    }

    Module* take(std::unique_ptr<Module> m) {
        Module* raw = m.get();
        owned.push_back(std::move(m));
        return raw;
    }

    Module* make_data(const std::string& col) {
        if (!panel || !panel->has_column(col)) {
            // A bare name that exactly one wide dataset carries gets the dotted spelling as a hint.
            std::string hint;
            int matches = 0;
            if (panel && col.find('.') == std::string::npos)
                for (const auto& name : panel->column_names()) {
                    const auto dot = name.find('.');
                    if (dot != std::string::npos && name.compare(dot + 1, std::string::npos, col) == 0) {
                        hint = name;
                        ++matches;
                    }
                }
            throw std::runtime_error("expr_parser: unknown or disabled data field '" + col + "'" +
                                     (matches == 1 ? " (did you mean '" + hint + "'?)" : ""));
        }
        auto m = std::make_unique<MData>();
        m->column_name = col;
        m->panel = panel;
        return take(std::move(m));
    }

    Module* make_constant(float val) {
        auto m = std::make_unique<MConstant>();
        m->value = FloatParameter(val);
        return take(std::move(m));
    }

    Module* make_binop(Module* l, Module* r, TokKind op) {
        std::unique_ptr<MBinaryOp> m;
        switch (op) {
            case TokKind::Plus:  m = std::make_unique<MAdd>(); break;
            case TokKind::Minus: m = std::make_unique<MSub>(); break;
            case TokKind::Star:  m = std::make_unique<MMul>(); break;
            case TokKind::Slash: m = std::make_unique<MDiv>(); break;
            default: throw std::runtime_error("expr_parser: unexpected operator");
        }
        m->lhs = l;
        m->rhs = r;
        return take(std::move(m));
    }

    Module* parse_expr_node() {
        Module* lhs = parse_term();
        while (cur().kind == TokKind::Plus || cur().kind == TokKind::Minus) {
            TokKind op = cur().kind;
            consume();
            Module* rhs = parse_term();
            lhs = make_binop(lhs, rhs, op);
        }
        return lhs;
    }

    Module* parse_term() {
        Module* lhs = parse_unary();
        while (cur().kind == TokKind::Star || cur().kind == TokKind::Slash) {
            TokKind op = cur().kind;
            consume();
            Module* rhs = parse_unary();
            lhs = make_binop(lhs, rhs, op);
        }
        return lhs;
    }

    Module* parse_unary() {
        if (cur().kind == TokKind::Minus) {
            consume();
            Module* inner = parse_primary();
            Module* neg_one = make_constant(-1.0f);
            return make_binop(neg_one, inner, TokKind::Star);
        }
        return parse_primary();
    }

    // returns → close / Chain(close, Delay(1)) - 1.0
    Module* make_returns() {
        Module* close_num = make_data("close");
        Module* close_den = make_data("close");

        auto delay = std::make_unique<MDelay>();
        delay->days = IntParameter(1);
        Module* r_delay = take(std::move(delay));

        auto chain = std::make_unique<MChain>();
        chain->first = close_den;
        chain->second = r_delay;
        Module* r_chain = take(std::move(chain));

        Module* div_result = make_binop(close_num, r_chain, TokKind::Slash);
        Module* one_val = make_constant(1.0f);
        return make_binop(div_result, one_val, TokKind::Minus);
    }

    // Read directly as float (same reader as XML FloatParameter attributes); a decimal
    // outside float range is rejected by the reader itself.
    float parse_literal() {
        float v = 0.0f;
        if (!module_attributes::parse_decimal(cur().text, v))
            throw std::runtime_error("expr_parser: invalid numeric literal '" + cur().text + "'");
        consume();
        return v;
    }

    template <class Op>
    Module* make_unary(Module* arg, float param) {
        auto u = std::make_unique<MUnary<Op>>();
        u->param = FloatParameter(param);
        auto chain = std::make_unique<MChain>();
        chain->first = arg;
        chain->second = take(std::move(u));
        return take(std::move(chain));
    }

    // name '(' expr [',' ['-'] number] ')'; the '(' is the current token.
    Module* parse_call(const std::string& name) {
        const bool one = name == "abs" || name == "log" || name == "exp" || name == "sqrt" || name == "sign";
        const bool two = name == "power" || name == "signedpower";
        if (!one && !two)
            throw std::runtime_error("expr_parser: unknown function '" + name + "'");
        const std::string arity = one ? "' expects 1 argument" : "' expects 2 arguments";
        consume();  // '('
        if (cur().kind == TokKind::RParen || cur().kind == TokKind::Comma)
            throw std::runtime_error("expr_parser: '" + name + arity);
        Module* arg = parse_expr_node();
        float p = 0.0f;
        if (two) {
            if (cur().kind != TokKind::Comma)
                throw std::runtime_error("expr_parser: '" + name + arity);
            consume();
            const bool neg = cur().kind == TokKind::Minus;
            if (neg) consume();
            if (cur().kind != TokKind::Number)
                throw std::runtime_error("expr_parser: '" + name + "' exponent must be a number");
            p = parse_literal();
            if (neg) p = -p;
            if (cur().kind != TokKind::RParen && cur().kind != TokKind::Comma && cur().kind != TokKind::Eof)
                throw std::runtime_error("expr_parser: '" + name + "' exponent must be a number");
        }
        if (cur().kind == TokKind::Comma)
            throw std::runtime_error("expr_parser: '" + name + arity);
        expect(TokKind::RParen, "')'");
        if (name == "abs")   return make_unary<unary_ops::Abs>(arg, p);
        if (name == "log")   return make_unary<unary_ops::Log>(arg, p);
        if (name == "exp")   return make_unary<unary_ops::Exp>(arg, p);
        if (name == "sqrt")  return make_unary<unary_ops::Sqrt>(arg, p);
        if (name == "sign")  return make_unary<unary_ops::Sign>(arg, p);
        if (name == "power") return make_unary<unary_ops::Power>(arg, p);
        return make_unary<unary_ops::SignedPower>(arg, p);
    }

    Module* parse_primary() {
        if (cur().kind == TokKind::Number)
            return make_constant(parse_literal());
        if (cur().kind == TokKind::Ident) {
            std::string name = cur().text;
            consume();
            const bool dotted = name.find('.') != std::string::npos;
            if (cur().kind == TokKind::LParen) {
                if (dotted)
                    throw std::runtime_error("expr_parser: '" + name + "' is a data field, not a function");
                return parse_call(name);
            }
            if (dotted) return make_data(name);
            // A thin-v1 panel carries `returns` (total return) as a cache field; other layouts derive it from close.
            if (name == "returns")
                return panel && (panel->thin_layout || panel->has_column("returns")) ? make_data("returns") : make_returns();
            return make_data(name);
        }
        if (cur().kind == TokKind::LParen) {
            consume();
            Module* inner = parse_expr_node();
            expect(TokKind::RParen, "')'");
            return inner;
        }
        throw std::runtime_error("expr_parser: unexpected token '" + cur().text + "'");
    }
};

} // namespace

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

Module* parse_expr(const std::string& expr_str,
                   const DataPanel* panel,
                   std::vector<std::unique_ptr<Module>>& owned)
{
    auto a = expr_str.find_first_not_of(" \t\r\n");
    if (a == std::string::npos)
        throw std::runtime_error("expr_parser: empty expression");
    auto b = expr_str.find_last_not_of(" \t\r\n");
    std::string trimmed = expr_str.substr(a, b - a + 1);

    auto toks = tokenise(trimmed);
    Parser p{toks, 0, panel, owned};
    Module* root = p.parse_expr_node();
    if (p.cur().kind != TokKind::Eof)
        throw std::runtime_error("expr_parser: unexpected tokens after expression");

    return root;
}

// ---------------------------------------------------------------------------
// Helpers: optional attribs with default, parsed strictly by module_attributes
// ---------------------------------------------------------------------------

static int opt_int(const std::map<std::string, std::string>& a, const std::string& tag,
                   const std::string& key, int def) {
    auto it = a.find(key);
    return it == a.end() ? def : module_attributes::parse_int(it->second, tag, key);
}

static float opt_float(const std::map<std::string, std::string>& a, const std::string& tag,
                       const std::string& key, float def) {
    auto it = a.find(key);
    return it == a.end() ? def : module_attributes::parse_float(it->second, tag, key);
}

static bool opt_bool(const std::map<std::string, std::string>& a, const std::string& tag,
                     const std::string& key, bool def) {
    auto it = a.find(key);
    return it == a.end() ? def : module_attributes::parse_bool(it->second, tag, key);
}

static int panel_time_limit(const DataPanel* panel) {
    if (panel == nullptr)
        throw CompileError("DataPanel is required for compile-time range validation");
    if (panel->T > static_cast<unsigned>(std::numeric_limits<int>::max()))
        throw CompileError("DataPanel T exceeds supported range for compile-time validation");
    return static_cast<int>(panel->T);
}

static std::string make_range(int min_value, int max_value) {
    return "[" + std::to_string(min_value) + ", " + std::to_string(max_value) + "]";
}

static std::string trim_ascii_whitespace(const std::string& value) {
    size_t begin = 0;
    while (begin < value.size() &&
           std::isspace(static_cast<unsigned char>(value[begin]))) {
        ++begin;
    }

    size_t end = value.size();
    while (end > begin &&
           std::isspace(static_cast<unsigned char>(value[end - 1]))) {
        --end;
    }

    return value.substr(begin, end - begin);
}

static int parse_bounded_int(const std::string& tag,
                             const std::string& attribute,
                             const std::string& raw_value,
                             int min_value,
                             int max_value) {
    int parsed = 0;
    const std::string trimmed = trim_ascii_whitespace(raw_value);
    const char* last = trimmed.data() + trimmed.size();
    const auto result = std::from_chars(trimmed.data(), last, parsed, 10);
    if (trimmed.empty() || result.ec != std::errc() || result.ptr != last) {
        throw CompileError(
            "Invalid value for <" + tag + "> attribute '" + attribute + "': '" + raw_value +
            "' (expected integer in range " + make_range(min_value, max_value) + ")");
    }

    if (parsed < min_value || parsed > max_value) {
        throw CompileError(
            "Invalid value for <" + tag + "> attribute '" + attribute + "': '" + raw_value +
            "' (expected range " + make_range(min_value, max_value) + ")");
    }
    return parsed;
}

static int get_bounded_int(const std::map<std::string, std::string>& attribs,
                           const std::string& tag,
                           const std::string& attribute,
                           int default_value,
                           int min_value,
                           int max_value) {
    auto it = attribs.find(attribute);
    if (it == attribs.end())
        return parse_bounded_int(
            tag, attribute, std::to_string(default_value), min_value, max_value);
    return parse_bounded_int(tag, attribute, it->second, min_value, max_value);
}

static std::string xml_escape(const std::string& v) {
    std::string out;
    out.reserve(v.size());
    for (char c : v) {
        switch (c) {
        case '&': out += "&amp;"; break;
        case '<': out += "&lt;"; break;
        case '>': out += "&gt;"; break;
        case '"': out += "&quot;"; break;
        case '\'': out += "&apos;"; break;
        default: out += c; break;
        }
    }
    return out;
}

static std::string pipeline_to_xml(const Pipeline& ops) {
    std::ostringstream oss;
    for (size_t i = 0; i < ops.size(); ++i) {
        const auto& op = ops[i];
        oss << "<" << op.first;
        for (const auto& kv : op.second)
            oss << " " << kv.first << "=\"" << xml_escape(kv.second) << "\"";
        oss << "/>";
        if (i + 1 < ops.size())
            oss << "\n";
    }
    return oss.str();
}

// ---------------------------------------------------------------------------
// ModuleRegistry (STAR-style: default-construct + field injection)
// ---------------------------------------------------------------------------

void ModuleRegistry::register_module(const std::string& tag, ModuleFactory factory) {
    if (!factories_.emplace(tag, std::move(factory)).second)
        throw CompileError("Duplicate module tag: '" + tag + "'");
}

std::unique_ptr<Module> ModuleRegistry::create(
    const std::string& tag,
    const std::map<std::string, std::string>& attribs,
    const DataPanel* panel) const
{
    auto it = factories_.find(tag);
    if (it == factories_.end())
        throw CompileError("Unknown module tag: '" + tag + "'");
    return it->second(attribs, panel);
}

// Shared group validation for neutralize, group_* and discovered GroupParameter
// fields; `tag` is substituted into the messages (neutralize texts unchanged).
// Returns the group name, which is also the panel field name for every supported group.
std::string validate_group_attribute(const std::map<std::string, std::string>& a,
                                     const std::string& tag, const DataPanel* panel,
                                     const std::string& key) {
    auto it = a.find(key);
    if (it == a.end())
        throw CompileError(
            "<" + tag + "> requires attribute '" + key + "' (sector|industry)");
    // Trim like parse_bounded_int does for numeric attributes.
    const std::string group = trim_ascii_whitespace(it->second);
    if (group != "sector" && group != "industry")
        throw CompileError(
            "Unsupported " + tag + " group '" + group +
            "' (supported: sector, industry)");
    if (panel == nullptr)
        throw CompileError(
            "DataPanel is required for <" + tag + "> group validation");
    // group name == panel field name for every supported group
    const std::string& column = group;
    if (!panel->has_column(column))
        throw CompileError(
            tag + " group '" + group + "' needs data field '" + column +
            "' (add base: {cache, groups} to the config)");
    return group;
}

ModuleRegistry& ModuleRegistry::default_registry() {
    static ModuleRegistry reg = [](){
        ModuleRegistry r;

        r.register_module("tsmean", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days"}, "tsmean");
            auto m = std::make_unique<MTsMean>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsmean", "days", 20, 1, max_days));
            return m;
        });

        r.register_module("tszscore", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "ddof", "min_count", "eps"}, "tszscore");
            auto m = std::make_unique<MTsZscore>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tszscore", "days", 20, 1, max_days));
            m->ddof = IntParameter(opt_int(a, "tszscore", "ddof", 1));
            m->min_count = IntParameter(opt_int(a, "tszscore", "min_count", -1));
            m->eps = FloatParameter(opt_float(a, "tszscore", "eps", 1e-12f));
            return m;
        });

        r.register_module("tsrank", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "pct", "min_count"}, "tsrank");
            auto m = std::make_unique<MTsRank>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsrank", "days", 20, 1, max_days));
            m->pct = IntParameter(opt_bool(a, "tsrank", "pct", true) ? 1 : 0);

            m->min_count = IntParameter(opt_int(a, "tsrank", "min_count", -1));
            return m;
        });

        r.register_module("decay", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days"}, "decay");
            auto m = std::make_unique<MDecay>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "decay", "days", 20, 1, max_days));
            return m;
        });

        r.register_module("delay", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days"}, "delay");
            auto m = std::make_unique<MDelay>();
            const int max_delay = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "delay", "days", 1, 0, max_delay));
            return m;
        });

        r.register_module("rank", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"pct"}, "rank");
            auto m = std::make_unique<MRank>();
            m->panel = panel;
            m->pct = IntParameter(opt_bool(a, "rank", "pct", true) ? 1 : 0);
            return m;
        });

        r.register_module("zscore", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"ddof", "min_count", "eps"}, "zscore");
            auto m = std::make_unique<MZscore>();
            m->panel = panel;
            m->ddof = IntParameter(opt_int(a, "zscore", "ddof", 1));
            m->min_count = IntParameter(opt_int(a, "zscore", "min_count", 2));
            m->eps = FloatParameter(opt_float(a, "zscore", "eps", 1e-12f));
            return m;
        });

        // Element-wise arithmetic (MUnary<Op>); `key` names the required numeric attribute, if any.
        auto unary = [&r](const std::string& tag, auto op, const char* key) {
            r.register_module(tag, [tag, key](const auto& a, const auto*) {
                auto m = std::make_unique<MUnary<decltype(op)>>();
                if (key == nullptr) {
                    module_attributes::validate_keys(a, {}, tag);
                    return m;
                }
                module_attributes::validate_keys(a, {key}, tag);
                auto it = a.find(key);
                if (it == a.end())
                    throw CompileError("<" + tag + "> requires attribute '" + key + "'");
                m->param = FloatParameter(module_attributes::parse_float(it->second, tag, key));
                return m;
            });
        };
        unary("abs", unary_ops::Abs{}, nullptr);
        unary("log", unary_ops::Log{}, nullptr);
        unary("exp", unary_ops::Exp{}, nullptr);
        unary("sqrt", unary_ops::Sqrt{}, nullptr);
        unary("sign", unary_ops::Sign{}, nullptr);
        unary("inverse", unary_ops::Inverse{}, nullptr);
        unary("reverse", unary_ops::Reverse{}, nullptr);
        unary("power", unary_ops::Power{}, "exp");
        unary("signedpower", unary_ops::SignedPower{}, "exp");
        unary("max", unary_ops::Max{}, "value");
        unary("min", unary_ops::Min{}, "value");

        auto group_attr = [](const std::map<std::string, std::string>& a,
                             const std::string& tag, const DataPanel* panel) {
            return validate_group_attribute(a, tag, panel);
        };

        r.register_module("neutralize", [group_attr](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"group"}, "neutralize");
            const std::string group = group_attr(a, "neutralize", panel);
            auto m = std::make_unique<MNeutralize>();
            m->panel = panel;
            m->group = group;
            m->column_name = group;
            return m;
        });

        // Fractional difference and rolling PCA residual (the combo15 MR alpha)
        r.register_module("ffd", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"d", "thres"}, "ffd");
            (void)panel;
            auto m = std::make_unique<MFfd>();
            m->d = FloatParameter(opt_float(a, "ffd", "d", 0.24f));
            const float thres = opt_float(a, "ffd", "thres", 0.00001f);
            if (!(thres >= 0.0f))
                throw CompileError("Invalid value for <ffd> attribute 'thres': expected thres >= 0");
            m->thres = FloatParameter(thres);
            return m;
        });

        r.register_module("pcaresid", [group_attr](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"group", "train", "refit", "k", "var"}, "pcaresid");
            auto m = std::make_unique<MPcaResid>();
            m->panel = panel;
            if (a.count("group")) m->group = GroupParameter(group_attr(a, "pcaresid", panel));
            const int max_days = panel_time_limit(panel);
            m->train = IntParameter(get_bounded_int(a, "pcaresid", "train", 1260, 2, max_days));
            m->refit = IntParameter(get_bounded_int(a, "pcaresid", "refit", 252, 1, max_days));
            m->k = IntParameter(get_bounded_int(a, "pcaresid", "k", 0, 0, std::numeric_limits<int>::max()));
            const float var = opt_float(a, "pcaresid", "var", 0.9f);
            if (!(var > 0.0f && var < 1.0f))   // sklearn's variance mode; 1.0 is a rounding-dependent edge
                throw CompileError("Invalid value for <pcaresid> attribute 'var': expected 0 < var < 1");
            m->var = FloatParameter(var);
            return m;
        });

        r.register_module("affine", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"factor", "offset"}, "affine");
            (void)panel;
            auto m = std::make_unique<MAffine>();
            m->factor = FloatParameter(opt_float(a, "affine", "factor", 1.0f));
            m->offset = FloatParameter(opt_float(a, "affine", "offset", 0.0f));
            return m;
        });

        // Time-series tags
        r.register_module("tsdelta", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days"}, "tsdelta");
            auto m = std::make_unique<MTsDelta>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsdelta", "days", 1, 1, max_days));
            return m;
        });

        r.register_module("tssum", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "min_count"}, "tssum");
            auto m = std::make_unique<MTsSum>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tssum", "days", 20, 1, max_days));
            m->min_count = IntParameter(get_bounded_int(a, "tssum", "min_count", m->days, 1, m->days));
            return m;
        });

        r.register_module("tsstddev", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "ddof", "min_count"}, "tsstddev");
            auto m = std::make_unique<MTsStdDev>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsstddev", "days", 20, 1, max_days));
            // Default ddof=1 is kept for days=1 (always NaN); an explicit ddof must be < days.
            m->ddof = IntParameter(a.count("ddof")
                ? get_bounded_int(a, "tsstddev", "ddof", 1, 0, m->days - 1) : 1);
            m->min_count = IntParameter(get_bounded_int(a, "tsstddev", "min_count", m->days, 1, m->days));
            return m;
        });

        r.register_module("tsargmax", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "min_count"}, "tsargmax");
            auto m = std::make_unique<MTsArgMax>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsargmax", "days", 20, 1, max_days));
            m->min_count = IntParameter(get_bounded_int(a, "tsargmax", "min_count", m->days, 1, m->days));
            return m;
        });

        r.register_module("tsargmin", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "min_count"}, "tsargmin");
            auto m = std::make_unique<MTsArgMin>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsargmin", "days", 20, 1, max_days));
            m->min_count = IntParameter(get_bounded_int(a, "tsargmin", "min_count", m->days, 1, m->days));
            return m;
        });

        r.register_module("tsfill", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days"}, "tsfill");
            auto m = std::make_unique<MTsFill>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsfill", "days", 20, 1, max_days));
            return m;
        });

        // Cross-sectional and group tags
        r.register_module("winsorize", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"std"}, "winsorize");
            const float k = opt_float(a, "winsorize", "std", 4.0f);
            if (!(k > 0.0f))
                throw CompileError("Invalid value for <winsorize> attribute 'std': '" +
                                   a.at("std") + "' (expected > 0)");
            auto m = std::make_unique<MWinsorize>();
            m->panel = panel;
            m->std_mult = FloatParameter(k);
            return m;
        });

        r.register_module("normalize", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"usestd", "limit"}, "normalize");
            const float limit = opt_float(a, "normalize", "limit", 0.0f);
            if (!(limit >= 0.0f))
                throw CompileError("Invalid value for <normalize> attribute 'limit': '" +
                                   a.at("limit") + "' (expected >= 0)");
            auto m = std::make_unique<MNormalize>();
            m->panel = panel;
            m->usestd = IntParameter(opt_bool(a, "normalize", "usestd", false) ? 1 : 0);
            m->limit = FloatParameter(limit);
            return m;
        });

        r.register_module("hump", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"hump"}, "hump");
            const float h = opt_float(a, "hump", "hump", 0.01f);
            if (!(h > 0.0f))
                throw CompileError("Invalid value for <hump> attribute 'hump': '" +
                                   a.at("hump") + "' (expected > 0)");
            auto m = std::make_unique<MHump>();
            m->panel = panel;
            m->hump = FloatParameter(h);
            return m;
        });

        r.register_module("group_rank", [group_attr](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"group", "pct"}, "group_rank");
            auto m = std::make_unique<MGroupRank>();
            m->group = m->column_name = group_attr(a, "group_rank", panel);
            m->panel = panel;
            m->pct = IntParameter(opt_bool(a, "group_rank", "pct", true) ? 1 : 0);
            return m;
        });

        r.register_module("group_mean", [group_attr](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"group", "weight", "mean"}, "group_mean");
            auto m = std::make_unique<MGroupMean>();
            m->group = m->column_name = group_attr(a, "group_mean", panel);
            m->panel = panel;
            auto w = a.find("weight");
            const std::string weight = w == a.end() ? "equal" : trim_ascii_whitespace(w->second);
            if (weight != "equal" && weight != "cap")
                throw CompileError(
                    "Unsupported group_mean weight '" + weight + "' (supported: equal, cap)");
            if (weight == "cap" && !panel->has_column(m->weight_column))
                throw CompileError(
                    "group_mean weight 'cap' needs data field '" + m->weight_column +
                    "' (add it to the config)");
            m->cap_weight = IntParameter(weight == "cap" ? 1 : 0);
            auto mn = a.find("mean");
            const std::string mean = mn == a.end() ? "arithmetic" : trim_ascii_whitespace(mn->second);
            if (mean != "arithmetic" && mean != "harmonic")
                throw CompileError(
                    "Unsupported group_mean mean '" + mean + "' (supported: arithmetic, harmonic)");
            m->harmonic = IntParameter(mean == "harmonic" ? 1 : 0);
            return m;
        });

        r.register_module("group_zscore", [group_attr](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"group"}, "group_zscore");
            auto m = std::make_unique<MGroupZscore>();
            m->group = m->column_name = group_attr(a, "group_zscore", panel);
            m->panel = panel;
            return m;
        });

        r.register_module("rank_sharp", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"exp"}, "rank_sharp");
            const float e = opt_float(a, "rank_sharp", "exp", 2.0f);
            if (!(e > 0.0f))
                throw CompileError("Invalid value for <rank_sharp> attribute 'exp': '" +
                                   a.at("exp") + "' (expected > 0)");
            auto m = std::make_unique<MRankSharp>();
            m->panel = panel;
            m->exp = FloatParameter(e);
            return m;
        });

        r.register_module("tsmax", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "min_count"}, "tsmax");
            auto m = std::make_unique<MTsMax>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsmax", "days", 20, 1, max_days));
            m->min_count = IntParameter(get_bounded_int(a, "tsmax", "min_count", m->days, 1, m->days));
            return m;
        });

        r.register_module("tsmin", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "min_count"}, "tsmin");
            auto m = std::make_unique<MTsMin>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsmin", "days", 20, 1, max_days));
            m->min_count = IntParameter(get_bounded_int(a, "tsmin", "min_count", m->days, 1, m->days));
            return m;
        });

        r.register_module("tsewma", [](const auto& a, const auto* /*panel*/) {
            module_attributes::validate_keys(a, {"span", "halflife", "alpha", "adjust", "min_count"}, "tsewma");
            const int given = (int)a.count("span") + (int)a.count("halflife") + (int)a.count("alpha");
            if (given > 1)
                throw CompileError("<tsewma> takes at most one of 'span', 'halflife', 'alpha'");
            auto m = std::make_unique<MTsEwma>();
            m->span = FloatParameter(-1.0f);
            if (a.count("alpha")) {
                const float v = opt_float(a, "tsewma", "alpha", 0.0f);
                if (!(v > 0.0f && v <= 1.0f))
                    throw CompileError("Invalid value for <tsewma> attribute 'alpha': '" +
                                       a.at("alpha") + "' (expected 0 < alpha <= 1)");
                m->alpha = FloatParameter(v);
            } else if (a.count("halflife")) {
                const float v = opt_float(a, "tsewma", "halflife", 0.0f);
                if (!(v > 0.0f))
                    throw CompileError("Invalid value for <tsewma> attribute 'halflife': '" +
                                       a.at("halflife") + "' (expected > 0)");
                m->halflife = FloatParameter(v);
            } else {
                const float v = opt_float(a, "tsewma", "span", 20.0f);
                if (!(v >= 1.0f))
                    throw CompileError("Invalid value for <tsewma> attribute 'span': '" +
                                       a.at("span") + "' (expected >= 1)");
                m->span = FloatParameter(v);
            }
            m->adjust = IntParameter(opt_bool(a, "tsewma", "adjust", true) ? 1 : 0);
            const int mc = opt_int(a, "tsewma", "min_count", 1);
            if (mc < 1)
                throw CompileError("Invalid value for <tsewma> attribute 'min_count': '" +
                                   a.at("min_count") + "' (expected >= 1)");
            m->min_count = IntParameter(mc);
            return m;
        });

        r.register_module("truncate", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"max", "side", "post"}, "truncate");
            auto it = a.find("max");
            if (it == a.end())
                throw CompileError("<truncate> requires attribute 'max'");
            const float mx = module_attributes::parse_float(it->second, "truncate", "max");
            if (!(mx > 0.0f && mx <= 1.0f))
                throw CompileError("Invalid value for <truncate> attribute 'max': '" +
                                   it->second + "' (expected 0 < max <= 1)");
            auto m = std::make_unique<MTruncate>();
            m->panel = panel;
            m->maxw = FloatParameter(mx);
            m->side = IntParameter(get_bounded_int(a, "truncate", "side", 0, 0, 1));
            m->post = IntParameter(opt_bool(a, "truncate", "post", true) ? 1 : 0);
            return m;
        });

        register_discovered_modules(r);
        return r;
    }();
    return reg;
}

// ---------------------------------------------------------------------------
// compile_pipeline (STAR-style: field injection, no constructors)
// ---------------------------------------------------------------------------

static bool contains_meta(const std::vector<XmlElement>& elements) {
    for (const auto& element : elements)
        if (element.tag == "meta" || contains_meta(element.children)) return true;
    return false;
}

// Set finalizer tags: they turn the pending <meta> SET into a SIGNAL (see combine_spec.h).
static bool is_combine_tag(const std::string& tag) {
    return tag == "ew" || tag == "mvo" || tag == "minvar" || tag == "erc" || tag == "ivol" ||
           tag == "spca" || tag == "glasso";
}

static unsigned parse_combine_uint(const std::map<std::string, std::string>& attribs, const std::string& tag,
                                   const std::string& key, int min_value, bool required, unsigned fallback) {
    auto it = attribs.find(key);
    if (it == attribs.end()) {
        if (required) throw CompileError("<" + tag + "> requires attribute '" + key + "'");
        return fallback;
    }
    const std::string expected = "integer >= " + std::to_string(min_value);
    int value = 0;
    try {
        value = module_attributes::parse_int(it->second, tag, key);
    } catch (const CompileError&) {
        throw module_attributes::detail::invalid_value(it->second, tag, key, expected.c_str());
    }
    if (value < min_value)
        throw module_attributes::detail::invalid_value(it->second, tag, key, expected.c_str());
    return static_cast<unsigned>(value);
}

static CombineSpec parse_combine_spec(const std::string& tag, const std::map<std::string, std::string>& attribs) {
    CombineSpec spec;
    if (tag == "ew") {
        module_attributes::validate_keys(attribs, {"sign"}, "ew", false);
        if (attribs.count("sign")) {
            spec.method = CombineMethod::EwSign;
            spec.days = parse_combine_uint(attribs, tag, "sign", 1, true, 0);
        }
        return spec;
    }
    if (tag == "mvo" || tag == "minvar") {
        module_attributes::validate_keys(attribs, {"days", "shrink", "rebalance"}, tag, false);
        spec.method = tag == "mvo" ? CombineMethod::Mvo : CombineMethod::MinVar;
        auto it = attribs.find("shrink");
        if (it != attribs.end()) {
            const std::string value = module_attributes::detail::trim(it->second);
            if (value == "lw") spec.shrink = ShrinkMode::LedoitWolf;
            else if (value == "cc") spec.shrink = ShrinkMode::ConstantCorrelation;
            else if (value == "oas") spec.shrink = ShrinkMode::Oas;
            else {
                const char* expected = "a number in [0,1] or lw|cc|oas";
                float delta = 0.0f;
                try {
                    delta = module_attributes::parse_float(it->second, tag, "shrink");
                } catch (const CompileError&) {
                    throw module_attributes::detail::invalid_value(it->second, tag, "shrink", expected);
                }
                if (!(delta >= 0.0f && delta <= 1.0f))
                    throw module_attributes::detail::invalid_value(it->second, tag, "shrink", expected);
                spec.delta = delta;
            }
        }
    } else if (tag == "erc" || tag == "ivol") {
        module_attributes::validate_keys(attribs, {"days", "rebalance"}, tag, false);
        spec.method = tag == "erc" ? CombineMethod::Erc : CombineMethod::Ivol;
    } else if (tag == "spca") {
        module_attributes::validate_keys(attribs, {"days", "k", "rebalance"}, tag, false);
        spec.method = CombineMethod::Spca;
        spec.k = parse_combine_uint(attribs, tag, "k", 1, true, 0);
    } else {  // glasso
        module_attributes::validate_keys(attribs, {"days", "alpha", "rebalance"}, tag, false);
        spec.method = CombineMethod::Glasso;
        auto it = attribs.find("alpha");
        if (it == attribs.end()) throw CompileError("<glasso> requires attribute 'alpha'");
        const char* expected = "number > 0";
        float alpha = 0.0f;
        try {
            alpha = module_attributes::parse_float(it->second, tag, "alpha");
        } catch (const CompileError&) {
            throw module_attributes::detail::invalid_value(it->second, tag, "alpha", expected);
        }
        if (!(alpha > 0.0f)) throw module_attributes::detail::invalid_value(it->second, tag, "alpha", expected);
        spec.alpha = alpha;
    }
    spec.days = parse_combine_uint(attribs, tag, "days", 2, true, 0);
    spec.rebalance = parse_combine_uint(attribs, tag, "rebalance", 1, false, 1);
    return spec;
}

// Compile the member alphas of <meta expr="NAME"> into a MetaSet (state SET until finalized).
static MetaSet compile_meta_set(const std::map<std::string, std::string>& attribs,
                                const DataPanel* panel,
                                const std::vector<uint8_t>* universe,
                                const MetaResolver* resolver) {
    module_attributes::validate_keys(attribs, {"expr"}, "meta", false);
    auto it = attribs.find("expr");
    if (it == attribs.end())
        throw CompileError("<meta> requires attribute 'expr'");
    if (resolver == nullptr || !*resolver)
        throw CompileError("meta_dir is not configured");
    MetaSet set;
    set.name = it->second;
    std::vector<std::pair<std::string, std::string>> listed;
    try {
        listed = (*resolver)(set.name);
    } catch (const CompileError&) {
        throw;
    } catch (const std::runtime_error& e) {
        throw CompileError(e.what());
    }
    const std::string prefix = "meta '" + set.name + "' member ";
    for (const auto& [path, xml] : listed) {
        std::vector<CompiledAlpha> compiled;
        try {
            if (contains_meta(parse_xml(xml)))
                throw CompileError(prefix + path + ": nested <meta> is not allowed");
            compiled = compile_xml(xml, panel, universe, nullptr);
        } catch (const CompileError& e) {
            const std::string msg = e.what();
            if (msg.rfind(prefix, 0) == 0) throw;
            throw CompileError(prefix + path + ": " + msg);
        } catch (const std::runtime_error& e) {
            throw CompileError(prefix + path + ": " + e.what());
        }
        if (compiled.size() != 1)
            throw CompileError(prefix + path + " must compile to exactly one alpha (got " +
                               std::to_string(compiled.size()) + ")");
        set.member_paths.push_back(path);
        set.members.push_back(std::move(compiled.front()));
    }
    return set;
}

// Saved signals keep their own first valid bar, a load resumes from it. <event> reads saved
// signals, so from there on the running bar is the maximum so far.
unsigned first_valid_bar(const CompiledAlpha& alpha, unsigned start) {
    std::map<const MSignalSave*, unsigned> saved_by_module;
    unsigned running = start, result = start;
    for (const Module* module : alpha.pipeline) {
        if (std::find(alpha.chain_starts.begin(), alpha.chain_starts.end(), module) != alpha.chain_starts.end()) running = start;
        if (const auto* save = dynamic_cast<const MSignalSave*>(module)) saved_by_module[save] = running;
        else if (const auto* load = dynamic_cast<const MSignalLoad*>(module)) running = saved_by_module.at(load->source);
        else if (dynamic_cast<const MEvent*>(module)) running = result;
        unsigned long long lookback = module->lookback(running);
        for (const MetaSet& set : alpha.meta_sets) {
            if (set.source != module) continue;
            for (const CompiledAlpha& member : set.members)
                lookback = std::max<unsigned long long>(lookback, first_valid_bar(member, running) - running);
            // Weights in force come from the grid t0 + k * rebalance, t0 = sim_start + days (meta.cpp), and
            // must estimate on [t - days, t) of real member returns: from the bar after the last member is
            // valid. A member valid at the history start has only the unfunded simStart row before that.
            if (!set.combine.is_ew()) {
                const unsigned long long days = set.combine.days, rebalance = std::max(1u, set.combine.rebalance);
                const unsigned long long late = running + lookback - start;
                unsigned long long first = start + 1 + days + (late + rebalance - 1) / rebalance * rebalance;
                // History start 0 is simStart 1 (the grid above) or simStart 0, whose grid has t0 = days.
                if (start == 0 && late > 0) first = std::max(first, days + (late + rebalance) / rebalance * rebalance);
                lookback = first - running;
            }
        }
        running = saturating_bars(running + lookback);
        result = std::max(result, running);
    }
    return result;
}

CompiledAlpha compile_pipeline(const Pipeline& ops, const DataPanel* panel,
                               const std::vector<uint8_t>* universe,
                               const MetaResolver* resolver) {
    if (ops.empty())
        throw CompileError("Pipeline is empty");

    CompiledAlpha result;
    // cache_registry: MSignalSave* for input/signal/event cache restoration
    std::map<std::string, MSignalSave*> cache_registry;
    unsigned data_chain_count = 0;
    Module* current = nullptr;

    if (ops[0].first != "data" && ops[0].first != "meta")
        throw CompileError("First pipeline element must be 'data', got '" + ops[0].first + "'");
    const bool starts_with_meta = ops[0].first == "meta";

    // SET state: the last meta_sets entry has no source yet. Finalizing (explicit <ew/> or
    // implicit before any other op / at the end) appends its MMetaSource; state -> SIGNAL.
    bool pending_set = false;
    bool meta_chain_unnamed = false;  // current chain began with <meta>, no <signal id> yet
    auto finalize_set = [&](const CombineSpec& spec) {
        result.meta_sets.back().combine = spec;
        auto source = std::make_unique<MMetaSource>();
        source->name = result.meta_sets.back().name;
        result.meta_sets.back().source = source.get();
        current = source.get();
        result.pipeline.push_back(current);
        result.chain_starts.push_back(current);
        result.modules.push_back(std::move(source));
        pending_set = false;
    };

    const ModuleRegistry& reg = ModuleRegistry::default_registry();

    for (size_t i = 0; i < ops.size(); ++i) {
        const std::string& tag    = ops[i].first;
        const auto&        attribs = ops[i].second;

        if (is_combine_tag(tag)) {
            if (!pending_set)
                throw CompileError("<" + tag + "> requires an alpha set from <meta>");
            finalize_set(parse_combine_spec(tag, attribs));
            continue;
        }
        if (pending_set) finalize_set(CombineSpec{});

        // A <meta> chain left unnamed would run all its members for nothing.
        if ((tag == "meta" || tag == "data") && meta_chain_unnamed)
            throw CompileError("a <meta> chain must be named with <signal id> before another chain starts");
        if (tag == "signal") meta_chain_unnamed = false;

        if (tag == "meta") {
            meta_chain_unnamed = true;
            ++data_chain_count;
            result.meta_sets.push_back(compile_meta_set(attribs, panel, universe, resolver));
            pending_set = true;
            continue;
        }

        if (tag == "data") {
            if (starts_with_meta)
                throw CompileError("<data> cannot restart a pipeline that began with <meta>");
            ++data_chain_count;
            // A misspelt delay must not compile to delay 0 (same-bar look-ahead).
            module_attributes::validate_keys(attribs, {"expr", "delay"}, "data", false);
            std::string expr = "close";
            {
                auto it = attribs.find("expr");
                if (it != attribs.end()) expr = it->second;
            }
            current = parse_expr(expr, panel, result.modules);
            result.pipeline.push_back(current);
            result.chain_starts.push_back(current);

            {
                auto it = attribs.find("delay");
                if (it != attribs.end()) {
                    const int max_lookback = panel_time_limit(panel);
                    int d = parse_bounded_int("data", "delay", it->second, 0, max_lookback);
                    if (d != 0) {
                        auto delay_mod = std::make_unique<MDelay>();
                        delay_mod->days = IntParameter(d);
                        current = delay_mod.get();
                        result.pipeline.push_back(current);
                        result.modules.push_back(std::move(delay_mod));
                    }
                }
            }
            continue;
        }

        if (tag == "signal") {
            module_attributes::validate_keys(attribs, {"id"}, "signal", false);
            auto it = attribs.find("id");
            if (it == attribs.end())
                throw CompileError("<signal> tag missing 'id' attribute");
            const std::string& sig_id = it->second;
            if (cache_registry.count(sig_id))
                throw CompileError("Duplicate signal id '" + sig_id + "'");

            auto save = std::make_unique<MSignalSave>();
            save->label = sig_id;
            MSignalSave* save_ptr = save.get();
            result.pipeline.push_back(save_ptr);
            result.modules.push_back(std::move(save));
            cache_registry[sig_id] = save_ptr;

            continue;
        }

        if (tag == "event") {
            module_attributes::validate_keys(
                attribs, {"signal", "trigger", "threshold", "maxholddays", "betall"}, "event", false);
            std::string sig_id, trig_id;
            {
                auto it = attribs.find("signal");
                if (it != attribs.end()) sig_id = it->second;
            }
            {
                auto it = attribs.find("trigger");
                if (it != attribs.end()) trig_id = it->second;
            }

            const std::vector<float>* sig_cache = nullptr;
            const std::vector<float>* trig_cache = nullptr;
            bool use_current_signal = false;

            if (!sig_id.empty()) {
                auto it = cache_registry.find(sig_id);
                if (it == cache_registry.end())
                    throw CompileError("event: unknown signal id '" + sig_id + "'");
                sig_cache = &it->second->cache;
            } else {
                if (data_chain_count > 1) {
                    throw CompileError(
                        "event: 'signal' attribute required after data chain restart");
                }
                use_current_signal = true;
            }

            if (!trig_id.empty()) {
                auto it = cache_registry.find(trig_id);
                if (it == cache_registry.end())
                    throw CompileError("event: unknown trigger id '" + trig_id + "'");
                trig_cache = &it->second->cache;
            } else {
                throw CompileError("event: 'trigger' attribute required");
            }

            auto mod = std::make_unique<MEvent>();
            mod->signal_cache   = sig_cache;
            mod->trigger_cache  = trig_cache;
            mod->useCurrentSignal = IntParameter(use_current_signal ? 1 : 0);
            mod->threshold      = FloatParameter(opt_float(attribs, "event", "threshold", 0.0f));
            mod->maxHolddays    = IntParameter(opt_int(attribs, "event", "maxholddays", 5));
            mod->betAll         = IntParameter(opt_bool(attribs, "event", "betall", true) ? 1 : 0);
            current = mod.get();
            result.pipeline.push_back(current);
            result.modules.push_back(std::move(mod));
            continue;
        }

        {
            auto inp_it = attribs.find("input");
            if (inp_it != attribs.end()) {
                const std::string& input_id = inp_it->second;
                auto cache_it = cache_registry.find(input_id);
                if (cache_it == cache_registry.end())
                    throw CompileError("input: unknown signal id '" + input_id + "'");
                auto load = std::make_unique<MSignalLoad>();
                load->source = cache_it->second;
                result.pipeline.push_back(load.get());
                result.modules.push_back(std::move(load));
            }
        }

        auto mod = reg.create(tag, attribs, panel);
        current = mod.get();
        result.pipeline.push_back(current);
        result.modules.push_back(std::move(mod));
    }

    if (pending_set) finalize_set(CombineSpec{});
    result.output = current;
    return result;
}

// ---------------------------------------------------------------------------
// compile_xml
// ---------------------------------------------------------------------------

std::vector<CompiledAlpha> compile_xml(const std::string& xml, const DataPanel* panel,
                                       const std::vector<uint8_t>* universe,
                                       const MetaResolver* resolver) {
    auto elements = parse_xml(xml);
    auto pipelines = expand_pipelines(elements);

    std::vector<CompiledAlpha> result;
    result.reserve(pipelines.size());
    for (auto& pipe : pipelines) {
        auto compiled = compile_pipeline(pipe, panel, universe, resolver);
        compiled.source_xml = pipeline_to_xml(pipe);
        if (universe)
            for (auto& module : compiled.modules) {
                if (auto* m = dynamic_cast<MRank*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MZscore*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MNeutralize*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MWinsorize*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MNormalize*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MHump*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MGroupBase*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MTruncate*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MPcaResid*>(module.get())) m->universe = universe;
                else ModuleRegistry::default_registry().bind_universe(*module, universe);
            }
        result.push_back(std::move(compiled));
    }
    return result;
}
