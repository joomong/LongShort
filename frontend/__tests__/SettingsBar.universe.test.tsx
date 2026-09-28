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
});
