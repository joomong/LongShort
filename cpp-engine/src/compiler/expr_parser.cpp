#include "expr_parser.h"
#include "../data/data_panel.h"
#include "../modules/Data.h"
#include "../modules/Delay.h"
#include "../modules/Ops.h"
#include "../modules/Constant.h"
#include "../modules/Chain.h"
#include <cctype>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <vector>
#include <memory>

// ---------------------------------------------------------------------------
// Tokeniser
// ---------------------------------------------------------------------------

enum class TokKind { Ident, Number, Plus, Minus, Star, Slash, LParen, RParen, Eof };

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
        if (std::isdigit((unsigned char)c) || c == '.') {
            size_t start = i;
            while (i < src.size() && (std::isdigit((unsigned char)src[i]) || src[i] == '.'))
                ++i;
            toks.push_back({TokKind::Number, src.substr(start, i - start)});
            continue;
        }
        if (std::isalpha((unsigned char)c) || c == '_') {
            size_t start = i;
            while (i < src.size() && (std::isalnum((unsigned char)src[i]) || src[i] == '_'))
                ++i;
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
        if (!panel || !panel->has_column(col))
            throw std::runtime_error("expr_parser: unknown or disabled data field '" + col + "'");
        auto m = std::make_unique<MData>();
        m->column_name = col;
        m->panel = panel;
        return take(std::move(m));
    }

    Module* make_constant(double val) {
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
            Module* neg_one = make_constant(-1.0);
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
        Module* one_val = make_constant(1.0);
        return make_binop(div_result, one_val, TokKind::Minus);
    }

    Module* parse_primary() {
        if (cur().kind == TokKind::Number) {
            size_t consumed = 0;
            double v = 0.0;
            try {
                v = std::stod(cur().text, &consumed);
            } catch (const std::invalid_argument&) {
                throw std::runtime_error("expr_parser: invalid numeric literal '" + cur().text + "'");
            } catch (const std::out_of_range&) {
                throw std::runtime_error("expr_parser: numeric literal out of range '" + cur().text + "'");
            }
            if (consumed != cur().text.size() || !std::isfinite(v) ||
                std::fabs(v) > std::numeric_limits<float>::max())
                throw std::runtime_error("expr_parser: invalid numeric literal '" + cur().text + "'");
            consume();
            return make_constant(v);
        }
        if (cur().kind == TokKind::Ident) {
            std::string name = cur().text;
            consume();
            // A thin-v1 panel carries `returns` as a cache field; other layouts derive it from close.
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
