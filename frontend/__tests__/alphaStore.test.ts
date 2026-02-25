import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useAlphaStore } from '@/store/alphaStore';

vi.mock('@/lib/api', () => ({
  getDataRange: vi.fn(),
  getSymbols: vi.fn(),
  runBacktest: vi.fn(),
  saveAlpha: vi.fn(),
  getAlphaList: vi.fn(),
  getAlpha: vi.fn(),
}));

import { getDataRange, getSymbols } from '@/lib/api';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  vi.clearAllMocks();
  useAlphaStore.setState(initialState, true);
});

describe('fetchDataRange', () => {
  it('should set dataRange when API succeeds', async () => {
    const mockRange = { minDate: '2020-01-01', maxDate: '2025-01-28', transactionFeeRate: 0.001 };
    vi.mocked(getDataRange).mockResolvedValue(mockRange);

    await useAlphaStore.getState().fetchDataRange();

    expect(useAlphaStore.getState().dataRange).toEqual(mockRange);
  });

  it('should clamp osEnd to maxDate from API', async () => {
    const mockRange = { minDate: '2020-01-01', maxDate: '2025-01-28', transactionFeeRate: 0.001 };
    vi.mocked(getDataRange).mockResolvedValue(mockRange);

    const settingsBefore = { ...useAlphaStore.getState().settings };
    await useAlphaStore.getState().fetchDataRange();

    const settingsAfter = useAlphaStore.getState().settings;
    // Other settings should remain unchanged
    expect(settingsAfter.isStart).toBe(settingsBefore.isStart);
    expect(settingsAfter.isEnd).toBe(settingsBefore.isEnd);
    expect(settingsAfter.osStart).toBe(settingsBefore.osStart);
    // But osEnd should be clamped to API maxDate
    expect(settingsAfter.osEnd).toBe(mockRange.maxDate);
  });

  it('should keep dataRange null on API failure', async () => {
    vi.mocked(getDataRange).mockRejectedValue(new Error('network error'));

    await useAlphaStore.getState().fetchDataRange();

    expect(useAlphaStore.getState().dataRange).toBeNull();
  });
});

describe('fetchSymbols', () => {
  it('should set symbols array when API succeeds', async () => {
    const mockSymbols = ['BTCUSDT', 'ETHUSDT'];
    vi.mocked(getSymbols).mockResolvedValue(mockSymbols);

    await useAlphaStore.getState().fetchSymbols();

    expect(useAlphaStore.getState().symbols).toEqual(mockSymbols);
  });

  it('should keep empty array on API failure', async () => {
    vi.mocked(getSymbols).mockRejectedValue(new Error('network error'));

    await useAlphaStore.getState().fetchSymbols();

    expect(useAlphaStore.getState().symbols).toEqual([]);
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
