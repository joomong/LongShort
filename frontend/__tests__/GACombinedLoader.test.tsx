import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import GACombinedLoader from '@/components/visualization/GACombinedLoader';
import { runCombinedAlphas } from '@/lib/api';
import { useAlphaStore } from '@/store/alphaStore';
import { multiAlphaMetricFixture } from './metricFixtures';

vi.mock('@/lib/api', () => ({ runCombinedAlphas: vi.fn(), getConfig: vi.fn() }));
const initial = useAlphaStore.getState();
const settings = { simStart: '2020-01-01', isStart: '2021-01-01', osStart: '2024-01-01', threads: 20 };
const candidate = (id: number) => ({ ...multiAlphaMetricFixture.alphas[0], candidateId: id, round: 1, xml: `<data expr="close"/><tsmean days="${id + 1}"/>` });
const combined = { ...candidate(0), name: 'combined', rank: 0, detailLoaded: true };
beforeEach(() => { vi.clearAllMocks(); useAlphaStore.setState(initial, true); });

it('waits for round completion, accumulates in stable order and reuses the same Stats aggregate', async () => {
  vi.mocked(runCombinedAlphas).mockResolvedValue(combined);
  render(<GACombinedLoader />);
  act(() => {
    const store = useAlphaStore.getState(); store.setIsRunning(true); store.beginGA(settings); store.startGARound(1);
    store.appendGACandidate(candidate(8)); store.appendGACandidate(candidate(3));
  });
  expect(runCombinedAlphas).not.toHaveBeenCalled();
  act(() => useAlphaStore.getState().endGARound(1));
  await waitFor(() => expect(useAlphaStore.getState().result?.combined?.detailLoaded).toBe(true));
  expect(runCombinedAlphas).toHaveBeenCalledWith([candidate(3).xml, candidate(8).xml], settings, expect.any(AbortSignal));
  act(() => { useAlphaStore.getState().finishGA(); useAlphaStore.getState().setIsRunning(false); });
  expect(useAlphaStore.getState().activeResultTab).toBe('stats');
  expect(useAlphaStore.getState().result?.combined).toEqual(combined);
  expect(runCombinedAlphas).toHaveBeenCalledTimes(1);
});

it('aborts obsolete aggregates and never attaches them to a new GA', async () => {
  let resolve!: (value: typeof combined) => void;
  vi.mocked(runCombinedAlphas).mockReturnValue(new Promise(done => { resolve = done; }));
  render(<GACombinedLoader />);
  act(() => {
    const store = useAlphaStore.getState(); store.beginGA(settings); store.startGARound(1);
    store.appendGACandidate(candidate(0)); store.appendGACandidate(candidate(1)); store.stopGA();
  });
  await waitFor(() => expect(runCombinedAlphas).toHaveBeenCalledTimes(1));
  const signal = vi.mocked(runCombinedAlphas).mock.calls[0][2];
  act(() => useAlphaStore.getState().beginGA(settings));
  await act(async () => { resolve(combined); });
  expect(signal?.aborted).toBe(true);
  expect(useAlphaStore.getState().result?.combined).toBeNull();
  expect(useAlphaStore.getState().gaCombinedCache).toEqual({});
});
