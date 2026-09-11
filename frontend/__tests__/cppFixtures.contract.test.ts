import { describe, expect, it } from 'vitest';
import { singleStarFixture, singleAlphaFixture, multiStarFixture, multiAlphaFixture, gaCompleteFixture, gaAlphaFixture } from './pipelineFixtures';

describe('actual C++ response fixtures', () => {
  it('maps count metrics and preserves the first trade return and IS/OS boundary', () => {
    const wire = singleStarFixture.batches[0];
    const alpha = singleAlphaFixture.alphas[0];
    expect(alpha.isMetrics.avgPositionCount).toBe(wire.summary.is.minBet);
    expect(alpha.isMetrics.minPositionCount).toBe(wire.summary.is.minBetMin);
    expect(alpha.isMetrics.activeBarCount).toBe(wire.summary.is.nonzeroPosition);
    const ts = wire.timeseries!;
    const first = wire.isStartIndex!;
    const os = wire.osStartIndex!;
    expect(alpha.isRetSeries?.[ts.timestamps[first]]).toBe(ts.cumPnl[first]);
    expect(alpha.osRetSeries?.[ts.timestamps[os]]).toBe(ts.cumPnl[os] - ts.cumPnl[os - 1]);
    expect(singleAlphaFixture.heatmap).toBe(alpha.heatmap);
  });
  it('keeps distinct combined and per-batch signals including sampling metadata', () => {
    expect(multiAlphaFixture.alphas).toHaveLength(2);
    expect(multiAlphaFixture.combined?.heatmap).toEqual(multiStarFixture.combined?.signalPlot);
    expect(multiAlphaFixture.heatmap).toBe(multiAlphaFixture.combined?.heatmap);
    multiAlphaFixture.alphas.forEach((alpha, index) => {
      expect(alpha.heatmap).toEqual(multiStarFixture.batches[index].signalPlot);
      expect(alpha.signalRange).toEqual(multiStarFixture.batches[index].signalRange);
      expect(alpha.distribution).toEqual(multiStarFixture.batches[index].distribution?.counts);
    });
    expect(multiAlphaFixture.heatmap?.sampled).toBe(true);
    expect(multiAlphaFixture.heatmap?.timestamps.at(-1)).toBe('2025-12-31');
  });
  it('adapts a real GA completion to a single selectable signal payload', () => {
    expect(gaAlphaFixture.alphas).toHaveLength(1);
    expect(gaAlphaFixture.heatmap).toBe(gaAlphaFixture.alphas[0].heatmap);
    expect(gaAlphaFixture.heatmap).toEqual(gaCompleteFixture.signalPlot);
    expect(gaAlphaFixture.alphas[0].xml).toBe(gaCompleteFixture.bestXml);
  });
});
