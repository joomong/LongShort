import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import SettingsBar from '@/components/RunSettingsBar';
import { useRunStore } from '@/runStore';

const initialState = useRunStore.getState();
const baseConfig = {
  symbols: ['A'],
  simStart: '2020-01-01',
  simEnd: '2025-01-28',
  feeRate: 0.00015,
  barsPerYear: 252,
  T: 10,
  N: 3000,
  columns: [],
  reportNotional: 20_000_000,
  executionPrice: 'close' as const,
  executionPrices: ['close', 'ohlc4', 'vwap'] as Array<'close' | 'ohlc4' | 'vwap'>,
  universes: [] as string[],
  defaultUniverse: '',
};

describe('SettingsBar universe selector', () => {
  beforeEach(() => useRunStore.setState(initialState, true));

  it('lists the configured universes and stores the choice', () => {
    useRunStore.setState({
      config: {
        ...baseConfig,
        universes: ['TOP3000', 'TOP2000', 'TOP1000', 'TOP500'],
        defaultUniverse: 'TOP3000',
      },
      settings: { ...initialState.settings, universe: 'TOP3000' },
    });
    render(<SettingsBar />);
    expect(screen.queryByLabelText('Bar interval')).toBeNull();
    const select = screen.getByLabelText('Universe') as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      'TOP3000',
      'TOP2000',
      'TOP1000',
      'TOP500',
    ]);
    expect(select.value).toBe('TOP3000');
    fireEvent.change(select, { target: { value: 'TOP500' } });
    expect(useRunStore.getState().settings.universe).toBe('TOP500');
  });

  it('shows no selector for a single configured universe', () => {
    useRunStore.setState({ config: baseConfig });
    render(<SettingsBar />);
    expect(screen.queryByLabelText('Universe')).toBeNull();
  });

  it('selects execution per next run without changing the engine default', () => {
    useRunStore.setState({
      config: {
        ...baseConfig,
        executionPrice: 'close',
        executionPrices: ['close', 'ohlc4', 'vwap'],
      },
    });
    render(<SettingsBar />);
    const select = screen.getByLabelText('Execution price') as HTMLSelectElement;
    fireEvent.change(select, { target: { value: 'ohlc4' } });
    expect(useRunStore.getState().settings.executionPrice).toBe('ohlc4');
    expect(useRunStore.getState().config?.executionPrice).toBe('close');
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual([
      'Close',
      'OHLC4',
      'VWAP',
    ]);
    fireEvent.change(select, { target: { value: 'vwap' } });
    expect(useRunStore.getState().settings.executionPrice).toBe('vwap');
  });
});
