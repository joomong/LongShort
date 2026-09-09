// Usage: node docs/refactor_2026-09-09_frontend_check.cjs <typescript.js> <baseline-frontend>
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require(path.resolve(process.argv[2]));
const currentRoot = path.resolve(__dirname, '../frontend');
const baselineRoot = path.resolve(process.argv[3]);

function loadApi(root) {
  const source = fs.readFileSync(path.join(root, 'lib/api.ts'), 'utf8');
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const module = { exports: {} };
  new Function('exports', 'module', 'require', 'process', js)(module.exports, module, require, process);
  return module.exports;
}

function expectedAfterRefactor(old) {
  const result = structuredClone(old);
  for (const alpha of [...result.alphas, ...(result.combined ? [result.combined] : [])]) {
    delete alpha.isEquitySeries;
    delete alpha.osEquitySeries;
    for (const m of [alpha.isMetrics, alpha.osMetrics]) {
      m.avgPositionCount = m.min_bet;
      m.minPositionCount = m.minBetMin;
      m.activeBarCount = m.nonzeroPosition;
      delete m.min_bet;
      delete m.minBetMin;
      delete m.nonzeroPosition;
    }
  }
  return result;
}

const oldApi = loadApi(baselineRoot);
const newApi = loadApi(currentRoot);
const summary = {
  return: 12, sharpe: 1.5, mdd: -3, turnover: 20, maxWeight: 40,
  minBet: 3.5, pnl: 5, winRate: 50, margin: 0.01, longRatio: 0.6,
  shortRatio: 0.4, nonzeroPosition: 8, avgMaxBet: 20, minBetMin: 2,
  quality: 0.9, sharpe400: 1.4, sharpe80: 1.1, minsoc: 80,
};
const timeseries = {
  timestamps: ['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'],
  cumPnl: [0, 0.01, 0.015, 0.03], cumPnlGross: [0, 0.011, 0.016, 0.031],
  turnover: [10, 20, 10, 30], maxWeight: [20, 30, 25, 40],
  nonzeroCount: [1, 2, 2, 3], drawdown: [0, -0.2, -0.1, -0.3],
};
const base = {
  batches: [{ index: 0, summary: {
    is: summary, os: { ...summary, minBet: 5.25, minBetMin: 1, nonzeroPosition: 13 },
  } }],
  yearly: [], timeseries, isStartIndex: 0, osStartIndex: 2,
  signalPlot: { timestamps: ['2024-01-01'], symbols: ['X'], values: [[0.5]] },
  signalRange: { min: -1, max: 1 },
  distribution: { bins: [0.1], counts: [10], min: 0, max: 1 },
};
const customBatch = {
  index: 1, summary: { is: { ...summary, sharpe: 2 }, os: summary },
  timeseries: { ...timeseries, cumPnl: [-0.03, -0.02, 0.01, 0.005] },
  isStartIndex: 1, osStartIndex: 3,
};
const cases = [
  base,
  { ...base, isStartIndex: -3, osStartIndex: -1 },
  { ...base, isStartIndex: 1, osStartIndex: 3 },
  { ...base, osStartIndex: 100 },
  { ...base, isStartIndex: 100, osStartIndex: 101 },
  { ...base, batches: [...base.batches, customBatch] },
  { ...base, batches: [...base.batches, customBatch], combined: { ...customBatch, index: -1 } },
  { ...base, batches: [] },
];
for (const input of cases) {
  const snapshot = structuredClone(input);
  assert.deepStrictEqual(newApi.adaptSimulateResponse(input), expectedAfterRefactor(oldApi.adaptSimulateResponse(input)));
  assert.deepStrictEqual(input, snapshot, 'adapter must not mutate wire payloads');
}
const ga = { ...base, type: 'complete', bestXml: '<data expr="close"/>' };
assert.deepStrictEqual(newApi.adaptGACompleteToResponse(ga), expectedAfterRefactor(oldApi.adaptGACompleteToResponse(ga)));
const alpha = newApi.adaptSimulateResponse(base).alphas[0];
assert.equal(alpha.isMetrics.avgPositionCount, 3.5);
assert.equal(alpha.isMetrics.minPositionCount, 2);
assert.equal(alpha.isMetrics.activeBarCount, 8);
assert.equal(alpha.osMetrics.avgPositionCount, 5.25);
assert.equal(alpha.osMetrics.minPositionCount, 1);
assert.equal(alpha.osMetrics.activeBarCount, 13);
assert(Math.abs(alpha.osRetSeries['2024-01-03'] - 0.005) < 1e-15);
assert(Math.abs(alpha.osRetSeries['2024-01-04'] - 0.015) < 1e-15);
assert(!('isEquitySeries' in alpha));
assert(!('osEquitySeries' in alpha));
console.log('PASS: 9 baseline adapter parity scenarios, legacy wire mapping, split returns, and no wire mutation');
