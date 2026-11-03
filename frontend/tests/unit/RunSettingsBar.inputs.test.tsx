import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import SettingsBar from '@/components/RunSettingsBar';
import { useRunStore } from '@/runStore';

const initialState = useRunStore.getState();
const settings = () => useRunStore.getState().settings;

describe('SettingsBar GA integer inputs', () => {
  beforeEach(() =>
    useRunStore.setState(
      { ...initialState, settings: { ...initialState.settings, threads: 4, stopRound: 40 } },
      true,
    ),
  );

  it('ignores non-digit keystrokes and commits on Enter', async () => {
    render(<SettingsBar />);
    const input = screen.getByLabelText('Stop round') as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, '5e2');
    expect(input.value).toBe('52');
    await userEvent.type(input, '{Enter}');
    expect(settings().stopRound).toBe(52);
  });

  it('ignores a pasted decimal', async () => {
    render(<SettingsBar />);
    const input = screen.getByLabelText('Number of threads') as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.paste('12');
    expect(input.value).toBe('12');
    await userEvent.clear(input);
    await userEvent.paste('1.9');
    expect(input.value).toBe('');
    fireEvent.blur(input);
    expect(input.value).toBe('4');
    expect(settings().threads).toBe(4);
  });

  it('restores the previous value when cleared and blurred', async () => {
    render(<SettingsBar />);
    const input = screen.getByLabelText('Stop round') as HTMLInputElement;
    await userEvent.clear(input);
    expect(input.value).toBe('');
    expect(settings().stopRound).toBe(40);
    fireEvent.blur(input);
    expect(input.value).toBe('40');
    expect(settings().stopRound).toBe(40);
  });

  it('clamps out-of-range values on commit', async () => {
    render(<SettingsBar />);
    const input = screen.getByLabelText('Number of threads') as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, '999');
    fireEvent.blur(input);
    expect(settings().threads).toBe(32);
    expect(input.value).toBe('32');
    await userEvent.clear(input);
    await userEvent.type(input, '0');
    fireEvent.blur(input);
    expect(settings().threads).toBe(1);
  });

  it('stays disabled while running', () => {
    useRunStore.setState({ isRunning: true });
    render(<SettingsBar />);
    expect((screen.getByLabelText('Stop alphas') as HTMLInputElement).disabled).toBe(true);
  });
});
