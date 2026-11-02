import { singleAlphaMetricFixture, multiAlphaMetricFixture } from './metricFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useAlphaStore } from '@/store/alphaStore';

vi.mock('@/lib/api', () => ({
  getConfig: vi.fn(),
  runBacktest: vi.fn(),
  saveAlpha: vi.fn(),
  getAlphaList: vi.fn(),
  getAlpha: vi.fn(),
}));

import { getConfig } from '@/lib/api';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  vi.clearAllMocks();
  useAlphaStore.setState(initialState, true);
});

describe('fetchConfig', () => {
  it('should set config when API succeeds', async () => {
    const mockConfig = {
      symbols: ['BTCUSDT', 'ETHUSDT'],
      simStart: '2020-01-01', simEnd: '2025-01-28',
      feeRate: 0.001,
      barsPerYear: 365,
      T: 1000,
      N: 2,
      columns: ['open', 'high', 'low', 'close', 'volume'],
    };
    vi.mocked(getConfig).mockResolvedValue(mockConfig);

    await useAlphaStore.getState().fetchConfig();

    expect(useAlphaStore.getState().config).toEqual(mockConfig);
  });

  it('should update settings isStart and osStart when out of range', async () => {
    const mockConfig = {
      symbols: ['BTCUSDT'],
      simStart: '2020-01-01', simEnd: '2025-01-28',
      feeRate: 0.001,
      barsPerYear: 365,
      T: 1000,
      N: 1,
      columns: [],
    };
    vi.mocked(getConfig).mockResolvedValue(mockConfig);

    // Set settings well outside the data range
    useAlphaStore.setState({
      settings: {
        simStart: '2018-01-01',
        simEnd: '',
        isStart: '2018-01-01',
        osStart: '2018-07-01',

        executionPrice: 'close',
        universe: '',
        nJobs: 5,
        gaPopulation: 50,
      },
    });

    await useAlphaStore.getState().fetchConfig();

    const settings = useAlphaStore.getState().settings;
    expect(settings.isStart >= mockConfig.simStart).toBe(true);
    expect(settings.osStart >= mockConfig.simStart).toBe(true);
    expect(settings.isStart < settings.osStart).toBe(true);
  });

  it('keeps the default split when only the start is clamped', async () => {
    vi.mocked(getConfig).mockResolvedValue({
      symbols: ['A'], simStart: '2016-01-04', simEnd: '2026-09-15', feeRate: 0.001, barsPerYear: 252, T: 10, N: 1, columns: [],
    });
    expect(useAlphaStore.getState().settings).toMatchObject({ simStart: '2015-01-01', isStart: '2016-01-01', osStart: '2024-01-01' });
    await useAlphaStore.getState().fetchConfig();
    expect(useAlphaStore.getState().settings).toMatchObject({
      simStart: '2016-01-04', isStart: '2016-01-04', osStart: '2024-01-01', simEnd: '2026-09-15' });
  });

  it('resets the split when it lies entirely before the data window', async () => {
    vi.mocked(getConfig).mockResolvedValue({
      symbols: ['A'], simStart: '2020-01-01', simEnd: '2025-01-28', feeRate: 0.001, barsPerYear: 252, T: 10, N: 1, columns: [],
    });
    useAlphaStore.getState().setSettings({ simStart: '2018-01-01', simEnd: '', isStart: '2018-01-01', osStart: '2018-07-01' });
    await useAlphaStore.getState().fetchConfig();
    expect(useAlphaStore.getState().settings).toMatchObject({
      simStart: '2020-01-01', isStart: '2020-01-01', osStart: '2023-07-22', simEnd: '2025-01-28' });
  });

  it('resets the split when the data window starts after the default osStart', async () => {
    vi.mocked(getConfig).mockResolvedValue({
      symbols: ['A'], simStart: '2024-06-03', simEnd: '2026-09-15', feeRate: 0.001, barsPerYear: 252, T: 10, N: 1, columns: [],
    });
    await useAlphaStore.getState().fetchConfig();
    expect(useAlphaStore.getState().settings).toMatchObject({
      simStart: '2024-06-03', isStart: '2024-06-03', osStart: '2026-01-08', simEnd: '2026-09-15' });
  });

  it('resets the split when the data window ends before the default isStart', async () => {
    vi.mocked(getConfig).mockResolvedValue({
      symbols: ['A'], simStart: '2005-01-03', simEnd: '2015-06-30', feeRate: 0.001, barsPerYear: 252, T: 10, N: 1, columns: [],
    });
    await useAlphaStore.getState().fetchConfig();
    expect(useAlphaStore.getState().settings).toMatchObject({
      simStart: '2005-01-03', isStart: '2005-01-03', osStart: '2012-05-07', simEnd: '2015-06-30' });
  });

  it('selects the default universe and keeps a still-valid choice', async () => {
    const mockConfig = {
      symbols: ['A'], simStart: '2020-01-01', simEnd: '2025-01-28', feeRate: 0.001, barsPerYear: 252,
      T: 10, N: 1, columns: [], universes: ['TOP3000', 'TOP2000', 'TOP1000', 'TOP500'], defaultUniverse: 'TOP3000',
    };
    vi.mocked(getConfig).mockResolvedValue(mockConfig);
    await useAlphaStore.getState().fetchConfig();
    expect(useAlphaStore.getState().settings.universe).toBe('TOP3000');
    useAlphaStore.getState().setSettings({ universe: 'TOP500' });
    await useAlphaStore.getState().fetchConfig();
    expect(useAlphaStore.getState().settings.universe).toBe('TOP500');
  });

  it('should keep config null on API failure', async () => {
    vi.mocked(getConfig).mockRejectedValue(new Error('network error'));

    await useAlphaStore.getState().fetchConfig();

    expect(useAlphaStore.getState().config).toBeNull();
  });
});

describe('selectedAlphaIndices', () => {
  it('toggleAlphaSelection should add index when not present', () => {
    useAlphaStore.getState().toggleAlphaSelection(2);

    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([2]);
  });

  it('toggleAlphaSelection should remove index when already present', () => {
    useAlphaStore.setState({ selectedAlphaIndices: [1, 2, 3] });

    useAlphaStore.getState().toggleAlphaSelection(2);

    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([1, 3]);
  });

  it('clearAlphaSelection should reset to empty array', () => {
    useAlphaStore.setState({ selectedAlphaIndices: [1, 2, 3] });

    useAlphaStore.getState().clearAlphaSelection();

    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([]);
  });

  it('setResult should clear selectedAlphaIndices', () => {
    useAlphaStore.setState({ selectedAlphaIndices: [0, 1, 2], selectedAlphaIndexSet: new Set([0, 1, 2]) });

    useAlphaStore.getState().setResult(null);

    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([]);
    expect(useAlphaStore.getState().selectedAlphaIndexSet.size).toBe(0);
  });
});


describe('initial result view', () => {
  it('opens the top combined batch immediately for multiple alphas', () => {
    const combined = { ...multiAlphaMetricFixture.alphas[0], name: 'combined' };
    useAlphaStore.getState().setResult({ ...multiAlphaMetricFixture, combined });
    expect(useAlphaStore.getState()).toMatchObject({ viewMode: 'detail', selectedAlphaIndex: -1 });
  });
  it('falls back to the first alpha if no combined batch is returned', () => {
    useAlphaStore.getState().setResult(multiAlphaMetricFixture);
    expect(useAlphaStore.getState()).toMatchObject({ viewMode: 'detail', selectedAlphaIndex: 0 });
  });
  it('keeps single-run signal analysis and clears detail on reset', () => {
    useAlphaStore.getState().setResult(multiAlphaMetricFixture);
    useAlphaStore.getState().setResult(singleAlphaMetricFixture);
    expect(useAlphaStore.getState()).toMatchObject({ viewMode: 'single', selectedAlphaIndex: null });
    useAlphaStore.getState().setResult(null);
    expect(useAlphaStore.getState()).toMatchObject({ viewMode: 'single', selectedAlphaIndex: null, result: null });
  });
});


describe('batch selection', () => {
  it('fills a partial selection, then clears all without changing the detail', () => {
    useAlphaStore.getState().setResult(multiAlphaMetricFixture);
    useAlphaStore.getState().toggleAlphaSelection(1);
    const selectedDetail = useAlphaStore.getState().selectedAlphaIndex;
    useAlphaStore.getState().toggleAllAlphaSelection();
    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([0, 1]);
    expect(useAlphaStore.getState().selectedAlphaIndexSet).toEqual(new Set([0, 1]));
    expect(useAlphaStore.getState().selectedAlphaIndex).toBe(selectedDetail);
    useAlphaStore.getState().toggleAllAlphaSelection();
    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([]);
    expect(useAlphaStore.getState().selectedAlphaIndexSet.size).toBe(0);
  });
});
