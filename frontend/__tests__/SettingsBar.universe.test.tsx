import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import SettingsBar from '@/components/visualization/SettingsBar';
import { useAlphaStore } from '@/store/alphaStore';

const initialState = useAlphaStore.getState();
const baseConfig = {
  symbols: ['A'], simStart: '2020-01-01', simEnd: '2025-01-28', feeRate: 0.00015, barsPerYear: 252,
  T: 10, N: 3000, columns: [],
};

describe('SettingsBar universe selector', () => {
  beforeEach(() => useAlphaStore.setState(initialState, true));

  it('lists the configured universes and stores the choice', () => {
    useAlphaStore.setState({
      config: { ...baseConfig, universes: ['TOP3000', 'TOP2000', 'TOP1000', 'TOP500'], defaultUniverse: 'TOP3000' },
      settings: { ...initialState.settings, universe: 'TOP3000' },
    });
    render(<SettingsBar />);
    expect(screen.queryByLabelText('Bar interval')).toBeNull();
    const select = screen.getByLabelText('Universe') as HTMLSelectElement;
    expect(Array.from(select.options).map(option => option.value)).toEqual(['TOP3000', 'TOP2000', 'TOP1000', 'TOP500']);
    expect(select.value).toBe('TOP3000');
    fireEvent.change(select, { target: { value: 'TOP500' } });
    expect(useAlphaStore.getState().settings.universe).toBe('TOP500');
  });

  it('shows no selector for a single configured universe', () => {
    useAlphaStore.setState({ config: baseConfig });
    render(<SettingsBar />);
    expect(screen.queryByLabelText('Universe')).toBeNull();
  });

  it('selects execution per run and disables unsupported older-server selection', () => {
    useAlphaStore.setState({ config: { ...baseConfig, executionPrice: 'close', executionPrices: ['close', 'ohlc4', 'vwap'] } });
    const view = render(<SettingsBar />);
    const select = screen.getByLabelText('Execution price') as HTMLSelectElement;
    expect(select.value).toBe('close');
    fireEvent.change(select, { target: { value: 'ohlc4' } });
    expect(useAlphaStore.getState().settings.executionPrice).toBe('ohlc4');
    expect(useAlphaStore.getState().config?.executionPrice).toBe('close');
    expect(Array.from(select.options).map(o => o.textContent)).toEqual(['Close', 'OHLC4', 'VWAP']);
    fireEvent.change(select, { target: { value: 'vwap' } });
    expect(useAlphaStore.getState().settings.executionPrice).toBe('vwap');
    fireEvent.change(select, { target: { value: 'close' } });
    expect(useAlphaStore.getState().settings.executionPrice).toBe('close');
    view.unmount();
    useAlphaStore.setState({ config: { ...baseConfig, executionPrice: 'ohlc4' } });
    render(<SettingsBar />);
    expect(screen.getByLabelText('Execution price')).toBeDisabled();
    expect(screen.getByLabelText('Execution price')).toHaveValue('ohlc4');
  });
});
