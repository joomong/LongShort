import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import GADetailLoader from '@/components/visualization/GADetailLoader';
import { useAlphaStore } from '@/store/alphaStore';
import { runSimulate } from '@/lib/api';
import fixture from './fixtures/cpp_single.json';
import { multiAlphaMetricFixture } from './metricFixtures';

vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), runSimulate: vi.fn() }));
const initial = useAlphaStore.getState();
beforeEach(() => { vi.clearAllMocks(); useAlphaStore.setState(initial, true); });

it('loads the selected daily detail after its round completes and repeated clicks do not reload it', async () => {
  vi.mocked(runSimulate).mockResolvedValue(fixture as never);
  render(<GADetailLoader />);
  act(() => {
    const store = useAlphaStore.getState();
    store.beginGA({ simStart: '2020-01-01', isStart: '2021-01-01', osStart: '2024-01-01', threads: 20 });
    store.startGARound(1);
    store.appendGACandidate({ ...multiAlphaMetricFixture.alphas[0], candidateId: 0, round: 1, xml: '<data/>', detailLoaded: false });
    store.enterDetailView(0);
  });
  expect(runSimulate).not.toHaveBeenCalled();
  expect(useAlphaStore.getState().viewMode).toBe('detail');
  act(() => useAlphaStore.getState().endGARound(1));
  await waitFor(() => expect(useAlphaStore.getState().result?.alphas[0].detailLoaded).toBe(true));
  expect(useAlphaStore.getState().result?.alphas[0].xml).toBe('<data/>');
  act(() => useAlphaStore.getState().enterDetailView(0));
  expect(runSimulate).toHaveBeenCalledTimes(1);
  expect(useAlphaStore.getState()).toMatchObject({ selectedAlphaIndex: 0, viewMode: 'detail' });
});

it('keeps signals loaded first when the metrics-only detail arrives', async () => {
  // The engine's metrics-only body has no signal payload; its adapted nulls must not erase cached signals.
  const { signalPlot: _plot, signalRange: _range, distribution: _distribution, ...metricsOnly } = fixture;
  vi.mocked(runSimulate).mockResolvedValue(metricsOnly as never);
  const signals = {
    heatmap: fixture.signalPlot, signalRange: fixture.signalRange, distribution: fixture.distribution.counts,
    distributionBins: fixture.distribution.bins, distributionRange: { min: fixture.distribution.min, max: fixture.distribution.max },
    signalDistributionLog: { ...fixture.distribution }, distributionExamples: { symbol: 'AAA', date: '2024-01-02' },
  } as never;
  act(() => {
    const store = useAlphaStore.getState();
    store.beginGA({ simStart: '2020-01-01', isStart: '2021-01-01', osStart: '2024-01-01', threads: 20 });
    store.startGARound(1);
    store.appendGACandidate({ ...multiAlphaMetricFixture.alphas[0], candidateId: 0, round: 1, xml: '<data/>', detailLoaded: false });
    store.endGARound(1); store.finishGA();
    store.cacheAlphaSignals(0, signals, useAlphaStore.getState().resultRevision);
  });
  render(<GADetailLoader />);
  act(() => useAlphaStore.getState().enterDetailView(0));
  await waitFor(() => expect(useAlphaStore.getState().result?.alphas[0].detailLoaded).toBe(true));
  expect(useAlphaStore.getState().result?.alphas[0]).toMatchObject(signals);
});
