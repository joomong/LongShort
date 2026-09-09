#include <iostream>
#include <iomanip>
#include <stdexcept>
#include "data/data_panel.h"
#include "engine/simulator.h"
#include "compiler/expr_parser.h"
#include "modules/Data.h"
#include "modules/TsMean.h"
#include "modules/Zscore.h"

int main(int argc, char** argv) {
    std::cout << std::setprecision(10);
    if (argc > 1) {
        auto p = DataPanel::load(argv[1], {"open","high","low","close","volume"}, "time", "symbol");
        std::cout << "T=" << p.T << " N=" << p.N << " timestamp=" << p.timestamps.at(0)
                  << " close=" << p.at("close",0,0) << "\n";
        return 0;
    }
    const float nan = std::numeric_limits<float>::quiet_NaN();
    // Actual production module, reused with the same input twice.
    MTsMean m; m.days=IntParameter(2); m.numAssets=1;
    for(int run=0; run<2; ++run) {
        m.init();
        std::cout << "tsmean_run" << run+1 << ":";
        for(unsigned t=0; t<4; ++t) {
            float x=float(t+1); SignalVector s{&x,1}; m.calculate(t,s);
            std::cout << " " << x;
        }
        std::cout << "\n";
    }
    // A portfolio holds 50% long A before A leaves the selection universe.
    DataPanel panel; panel.T=4; panel.N=2; panel.symbols={"A","B"};
    panel.timestamps={0,86400000,172800000,259200000};
    panel.arrays["close"]={100,100,100,100,50,100,50,100};
    SimulatorConfig cfg{4,2,0,1,252,false,{}};
    const std::vector<std::vector<float>> signals={{1,-1},{1,-1},{nan,-1},{nan,-1}};
    auto unmasked=Simulator(panel,cfg).run_from_signals(signals);
    panel.arrays["close"][4]=nan; panel.arrays["close"][6]=nan;
    auto masked=Simulator(panel,cfg).run_from_signals(signals);
    std::cout << "exit_day_unmasked_ret=" << unmasked.ret[2]
              << " masked_ret=" << masked.ret[2] << "\n";
    // Inactive symbol reappears through rolling mean before cross-sectional zscore.
    MTsMean rolling; rolling.days=IntParameter(2); rolling.numAssets=3; rolling.init();
    MZscore z; z.numAssets=3; z.init();
    float row0[]={10,20,100}; SignalVector a{row0,3}; rolling.calculate(0,a);
    float row1[]={10,20,nan}; SignalVector b{row1,3}; rolling.calculate(1,b);
    std::cout << "rolling_inactive_value=" << row1[2];
    z.calculate(1,b);
    double gross=std::abs(row1[0])+std::abs(row1[1]);
    std::cout << " active_net_weight_after_zscore=" << (row1[0]+row1[1])/gross << "\n";
    // Numeric AFL field names parse as numeric constants even if panel has that field.
    panel.arrays["1005"]={7,8,9,10,11,12,13,14};
    std::vector<std::unique_ptr<Module>> owned;
    Module* expr=parse_expr("1005",&panel,owned); expr->numAssets=2; expr->init();
    float val[2]={0,0}; SignalVector v{val,2}; expr->calculate(0,v);
    std::cout << "expr_1005=" << val[0] << "," << val[1] << " expected_field=7,8\n";
}
