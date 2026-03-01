#include "expr_parser.h"
#include "../modules/Data.h"
#include "../modules/Delay.h"
#include "../modules/Ops.h"
#include "../modules/Constant.h"
#include <cctype>
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

    Module* parse_primary() {
        if (cur().kind == TokKind::Number) {
            double v = std::stod(cur().text);
            consume();
            return make_constant(v);
        }
        if (cur().kind == TokKind::Ident) {
            std::string name = cur().text;
            consume();
            static const char* allowed[] = {"open","high","low","close","volume", nullptr};
            bool ok = false;
            for (int i = 0; allowed[i]; ++i)
                if (name == allowed[i]) { ok = true; break; }
            if (!ok)
                throw std::runtime_error("expr_parser: unknown identifier '" + name + "'");
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
// MChain: runs two modules in sequence on the same SignalVector.
// ---------------------------------------------------------------------------

struct MChain : public Module {
    Module* first = nullptr;
    Module* second = nullptr;

    void init() override {
        first->numAssets = numAssets;
        first->init();
        second->numAssets = numAssets;
        second->init();
    }

    void calculate(unsigned di, SignalVector& output) override {
        first->calculate(di, output);
        second->calculate(di, output);
    }

    void save_state(std::ostream& os) const override {
        first->save_state(os);
        second->save_state(os);
    }

    void load_state(std::istream& is) override {
        first->load_state(is);
        second->load_state(is);
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

    // "return" → close / Delay(close, 1) - 1.0
    if (trimmed == "return") {
        auto close_num = std::make_unique<MData>();
        close_num->column_name = "close";
        close_num->panel = panel;

        auto close_den = std::make_unique<MData>();
        close_den->column_name = "close";
        close_den->panel = panel;

        auto delay = std::make_unique<MDelay>();
        delay->days = IntParameter(1);

        auto one = std::make_unique<MConstant>();
        one->value = FloatParameter(1.0);

        Module* r_close_num = close_num.get();
        Module* r_close_den = close_den.get();
        Module* r_delay     = delay.get();
        Module* r_one       = one.get();

        owned.push_back(std::move(close_num));
        owned.push_back(std::move(close_den));
        owned.push_back(std::move(delay));
        owned.push_back(std::move(one));

        auto chain = std::make_unique<MChain>();
        chain->first  = r_close_den;
        chain->second = r_delay;
        Module* r_chain = chain.get();
        owned.push_back(std::move(chain));

        auto div_mod = std::make_unique<MDiv>();
        div_mod->lhs = r_close_num;
        div_mod->rhs = r_chain;
        Module* r_div = div_mod.get();
        owned.push_back(std::move(div_mod));

        auto sub_mod = std::make_unique<MSub>();
        sub_mod->lhs = r_div;
        sub_mod->rhs = r_one;
        Module* r_sub = sub_mod.get();
        owned.push_back(std::move(sub_mod));

        return r_sub;
    }

    auto toks = tokenise(trimmed);
    Parser p{toks, 0, panel, owned};
    Module* root = p.parse_expr_node();
    if (p.cur().kind != TokKind::Eof)
        throw std::runtime_error("expr_parser: unexpected tokens after expression");

    return root;
}
