#pragma once
// SignalInputs: mixin for modules that read rows saved by <signal id> through the
// attributes signal_1 .. signal_K (numbered from 1 without gaps). The compiler resolves the
// ids, checks the count against min/max_signal_inputs() and binds the saved rows before init().
// Every read is the same bar: a saved row holds the value its <signal id> stored on this bar.
// The module's output depends only on the bound rows; the current chain value is overwritten.
#include <vector>

struct Module;

struct SignalInputs {
    virtual ~SignalInputs() = default;
    virtual unsigned min_signal_inputs() const = 0;
    virtual unsigned max_signal_inputs() const = 0;   // 0 = no upper bound
    std::vector<const std::vector<float>*> signal_rows;   // cache of each <signal id>, in signal_N order
    std::vector<const Module*> signal_sources;            // the saving modules, for first-valid-bar accounting
    const float* signal_row(size_t k) const { return signal_rows[k]->data(); }
    size_t signal_count() const { return signal_rows.size(); }
};
