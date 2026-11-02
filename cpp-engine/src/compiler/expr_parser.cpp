#include "expr_parser.h"
#include "module_attributes.h"
#include "../data/data_panel.h"
#include "../modules/Data.h"
#include "../modules/Delay.h"
#include "../modules/Ops.h"
#include "../modules/Constant.h"
#include "../modules/Chain.h"
#include "../modules/Unary.h"
#include <cctype>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <vector>
#include <memory>

// ---------------------------------------------------------------------------
// Tokeniser
// ---------------------------------------------------------------------------

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
