import { robustCrossSection } from '../lib/heatmapStats';

// Dates are independent: a worker receives whole cross sections, preserving
// the median/MAD population and the existing float32 output exactly.
self.onmessage = (event: MessageEvent<{ values: Float32Array; rows: number; columns: number }>) => {
  try {
    const { values, rows, columns } = event.data;
    const input = Array.from({ length: rows }, (_, row) => values.subarray(row * columns, (row + 1) * columns));
    const normalized = robustCrossSection(input, columns);
    const output = new Float32Array(rows * columns);
    normalized.forEach((row, index) => output.set(row, index * columns));
    self.postMessage({ values: output }, { transfer: [output.buffer] });
  } catch (error) {
    self.postMessage({ error: String(error) });
  }
};
