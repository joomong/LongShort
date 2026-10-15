import { robustCrossSection, type SignalRows } from './heatmapStats';

const cache = new WeakMap<SignalRows, Map<number, Float32Array[]>>();

export function normalizeHeatmap(values: SignalRows, columns: number) {
  const stored = cache.get(values)?.get(columns);
  if (stored) return { promise: Promise.resolve(stored), cancel() {} };
  if (typeof Worker === 'undefined' || values.length * columns < 500000) {
    return { promise: Promise.resolve(robustCrossSection(values, columns)), cancel() {} };
  }
  const count = Math.min(2, Math.max(1, navigator.hardwareConcurrency || 1), values.length);
  const workers: Worker[] = [];
  let cancelled = false;
  let rejectCancelled: (() => void) | undefined;
  const abort = new Promise<never>((_, reject) => { rejectCancelled = () => reject(new DOMException('Aborted', 'AbortError')); });
  const task = Promise.all(Array.from({ length: count }, (_, part) => {
    const begin = Math.floor(values.length * part / count);
    const end = Math.floor(values.length * (part + 1) / count);
    const flat = new Float32Array((end - begin) * columns);
    for (let row = begin; row < end; row++) for (let column = 0; column < columns; column++) {
      const value = values[row]?.[column];
      flat[(row - begin) * columns + column] = typeof value === 'number' ? value : NaN;
    }
    return new Promise<Float32Array[]>((resolve, reject) => {
      const worker = new Worker(new URL('../workers/heatmap.worker.ts', import.meta.url), { type: 'module' });
      workers.push(worker);
      worker.onerror = () => reject(new Error('Heatmap worker failed'));
      worker.onmessage = (event: MessageEvent<{ values?: Float32Array; error?: string }>) => {
        if (event.data.error || !event.data.values) { reject(new Error(event.data.error || 'No heatmap result')); return; }
        const result = event.data.values;
        resolve(Array.from({ length: end - begin }, (_, row) => result.subarray(row * columns, (row + 1) * columns)));
      };
      worker.postMessage({ values: flat, rows: end - begin, columns }, [flat.buffer]);
    });
  })).then(parts => {
    const rows = parts.flat();
    const sizes = cache.get(values) ?? new Map<number, Float32Array[]>();
    sizes.set(columns, rows);
    cache.set(values, sizes);
    return rows;
  }).catch(error => {
    if (cancelled) throw error;
    // Keep charts available in browsers that prohibit or cannot load workers.
    return robustCrossSection(values, columns);
  });
  const promise = Promise.race([task, abort]).finally(() => workers.forEach(worker => worker.terminate()));
  return {
    promise,
    cancel() { cancelled = true; workers.forEach(worker => worker.terminate()); rejectCancelled?.(); },
  };
}
