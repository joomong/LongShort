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
      simStart: '2020-01-01',
      simEnd: '2025-01-28',
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
      simStart: '2020-01-01',
      simEnd: '2025-01-28',
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
        isStart: '2018-01-01',
        osStart: '2018-07-01',
        barInterval: 'D1',
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
    useAlphaStore.setState({ selectedAlphaIndices: [0, 1, 2] });

    useAlphaStore.getState().setResult(null);

    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([]);
  });
});
