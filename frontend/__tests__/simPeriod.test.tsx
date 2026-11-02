import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import SettingsBar from '@/components/visualization/SettingsBar';
import { useBacktest } from '@/hooks/useBacktest';
import { enforceSettingsDates, useAlphaStore } from '@/store/alphaStore';
import { getConfig, runGAStream, adaptGACompleteToResponse } from '@/lib/api';
vi.mock('@/lib/api', () => ({ getConfig: vi.fn(), runSimulate: vi.fn(), runGAStream: vi.fn(), adaptSimulateResponse: vi.fn(), adaptGACompleteToResponse: vi.fn() }));
const initial = useAlphaStore.getState();
const config = { symbols: [], simStart: '2020-01-01', simEnd: '2025-12-31', supportsSimEnd: true, executionPrices: ['close', 'ohlc4'] as Array<'close' | 'ohlc4'>, feeRate: 0, barsPerYear: 252, T: 1, N: 1, columns: [] };
beforeEach(() => {
  vi.clearAllMocks();
  useAlphaStore.setState({ ...initial, config, settings: { ...initial.settings, simEnd: config.simEnd } }, true);
});
it('defaults From and IS start to January 2016, clamped to the server range', () => {
  expect(initial.settings).toMatchObject({ simStart: '2015-01-01', isStart: '2016-01-01' });
  expect(enforceSettingsDates(initial.settings, '2016-01-04', '2026-09-15')).toMatchObject({ simStart: '2016-01-04', isStart: '2016-01-04' });
});
it('preserves warmup and splits, resets excluded splits, clamps range and permits empty one-day OS', () => {
  const s = { ...useAlphaStore.getState().settings, simStart: '2020-01-01', isStart: '2023-01-01', osStart: '2024-01-01' };
  expect(enforceSettingsDates({ ...s, simEnd: '2024-06-01' })).toMatchObject({ simStart: s.simStart, isStart: s.isStart, osStart: s.osStart });
  expect(enforceSettingsDates({ ...s, simEnd: '2020-01-11' })).toMatchObject({ isStart: '2020-01-01', osStart: '2020-01-08' });
  expect(enforceSettingsDates({ ...s, simStart: '2030-01-01', simEnd: '2031-01-01' }, config.simStart, config.simEnd)).toMatchObject({ simStart: config.simEnd, simEnd: config.simEnd, isStart: config.simEnd, osStart: '2026-01-01' });
  const day = enforceSettingsDates({ ...s, simEnd: '2020-01-01' });
  expect(day).toMatchObject({ isStart: '2020-01-01', osStart: '2020-01-02' });
  expect(enforceSettingsDates(day)).toEqual(day);
});
it('disables To for older servers', () => {
  useAlphaStore.setState({ config: { ...config, supportsSimEnd: undefined } });
  render(<SettingsBar />);
  expect(screen.getByLabelText('Sim end date')).toBeDisabled();
  expect(screen.getByLabelText('Sim end date')).toHaveValue(config.simEnd);
});
it('sends toolbar dates and Execution through the default GA run and records actual dates', async () => {
  let hook!: ReturnType<typeof useBacktest>;
  function Harness() { hook = useBacktest(); return <SettingsBar />; }
  render(<Harness />);
  fireEvent.change(screen.getByLabelText('Sim start date'), { target: { value: '2020-02-01' } });
  fireEvent.change(screen.getByLabelText('Sim end date'), { target: { value: '2020-02-11' } });
  fireEvent.change(screen.getByLabelText('Execution price'), { target: { value: 'ohlc4' } });
  const echo = { simStart: '2020-02-03', simEnd: '2020-02-10' };
  vi.mocked(adaptGACompleteToResponse).mockReturnValue({ combined: null, alphas: [] });
  vi.mocked(runGAStream).mockImplementation(async (...args) => { args[7]({ type: 'complete', ...echo, bestXml: '<data/>' } as never); });
  await act(async () => hook.run());
  const args = vi.mocked(runGAStream).mock.calls[0];
  expect(args.slice(1, 4)).toEqual(['2020-02-01', '2020-02-01', '2020-02-08']);
  expect(args.slice(10)).toEqual(['ohlc4', '2020-02-11', { stopAlphas: 400, roundSize: 50 }]);
  expect(useAlphaStore.getState().result?.runSettings).toMatchObject(echo);
});

it('defaults To to available data end and clearing To restores that bound', async () => {
  useAlphaStore.setState({ config: null, settings: initial.settings });
  vi.mocked(getConfig).mockResolvedValue(config);
  await useAlphaStore.getState().fetchConfig();
  expect(useAlphaStore.getState().settings.simEnd).toBe(config.simEnd);
  render(<SettingsBar />);
  fireEvent.change(screen.getByLabelText('Sim end date'), { target: { value: '2024-06-01' } });
  expect(useAlphaStore.getState().settings.simEnd).toBe('2024-06-01');
  fireEvent.change(screen.getByLabelText('Sim end date'), { target: { value: '' } });
  expect(screen.getByLabelText('Sim end date')).toHaveValue(config.simEnd);
});
