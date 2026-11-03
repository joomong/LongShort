import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, expect, it } from 'vitest';
import SettingsBar from '@/components/RunSettingsBar';
import { useRunStore } from '@/runStore';
import { engineConfigFixture } from './metricFixtures';
const initial = useRunStore.getState();
beforeEach(() => {
  useRunStore.setState(initial, true);
  useRunStore.getState().setConfig(engineConfigFixture);
});
it('preserves warmup/splits and permits empty one-day OS through settings actions', () => {
  useRunStore
    .getState()
    .setSettings({
      simStart: '2020-01-01',
      isStart: '2023-01-01',
      osStart: '2024-01-01',
      simEnd: '2024-06-01',
    });
  expect(useRunStore.getState().settings).toMatchObject({
    simStart: '2020-01-01',
    isStart: '2023-01-01',
    osStart: '2024-01-01',
  });
  useRunStore.getState().setSettings({ simEnd: '2020-01-11' });
  expect(useRunStore.getState().settings).toMatchObject({
    isStart: '2020-01-01',
    osStart: '2020-01-08',
  });
  useRunStore.getState().setSettings({ simEnd: '2020-01-01' });
  expect(useRunStore.getState().settings).toMatchObject({
    isStart: '2020-01-01',
    osStart: '2020-01-02',
  });
});
it('defaults end to available data and clearing the input restores that bound', () => {
  expect(useRunStore.getState().settings.simEnd).toBe(engineConfigFixture.simEnd);
  render(<SettingsBar />);
  fireEvent.change(screen.getByLabelText('Sim end date'), { target: { value: '2024-06-01' } });
  expect(useRunStore.getState().settings.simEnd).toBe('2024-06-01');
  fireEvent.change(screen.getByLabelText('Sim end date'), { target: { value: '' } });
  expect(screen.getByLabelText('Sim end date')).toHaveValue(engineConfigFixture.simEnd);
});
