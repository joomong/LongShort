import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SaveAlphaModal from '@/components/modals/SaveAlphaModal';
import LoadAlphaModal from '@/components/modals/LoadAlphaModal';
import { useAlphaStore } from '@/store/alphaStore';
import type { BacktestRunResponse } from '@/types';

const initial = useAlphaStore.getState();
const originalFetch = globalThis.fetch;
const saved = new Map<string, string>();
const xml = '<data expr="(close-open)/open" delay="1"/>\n<rank pct="false"/>';
function setupResult(missing = false): BacktestRunResponse {
  return { combined: null, alphas: [
    { name: 'alpha_7', rank: 1, xml, isMetrics: { pnl: 1, sharpe: 1 }, osMetrics: { pnl: 1, sharpe: 1 } },
    { name: 'alpha_3', rank: 2, xml: missing ? undefined : '<data expr="-close" delay="2"/>', isMetrics: { pnl: 2, sharpe: 2 }, osMetrics: { pnl: 2, sharpe: 2 } },
  ] };
}
beforeEach(() => {
  saved.clear();
  useAlphaStore.setState(initial, true);
  globalThis.fetch = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') {
      const payload = JSON.parse(String(init.body)) as { name: string; xml: string };
      saved.set(payload.name, payload.xml);
      return { ok: true, json: async () => ({ saved: true, name: payload.name }) } as Response;
    }
    return { ok: true, json: async () => Array.from(saved).map(([name, xml]) => ({ name, xml })) } as Response;
  });
});
afterEach(() => { globalThis.fetch = originalFetch; });

describe('alpha XML save/load roundtrip', () => {
  it('saves each selected expanded XML and loads it without changing an explicit delay', async () => {
    useAlphaStore.getState().setResult(setupResult());
    useAlphaStore.getState().exitDetailView();
    useAlphaStore.getState().toggleAlphaSelection(0);
    useAlphaStore.getState().toggleAlphaSelection(1);
    useAlphaStore.getState().setIsSaveModalOpen(true);
    const saveView = render(<SaveAlphaModal />);
    fireEvent.change(screen.getByLabelText('Alpha Name'), { target: { value: 'desk' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(saved.size).toBe(2));
    expect(saved.get('desk_alpha_7')).toBe(xml);
    expect(saved.get('desk_alpha_3')).toBe('<data expr="-close" delay="2"/>');
    saveView.unmount();
    useAlphaStore.getState().setIsLoadModalOpen(true);
    render(<LoadAlphaModal />);
    const name = await screen.findByText('desk_alpha_7');
    fireEvent.click(within(name.closest('.items-center') as HTMLElement).getByRole('button', { name: 'Load' }));
    await waitFor(() => expect(useAlphaStore.getState().xmlCode).toBe(xml));
  });

  it('saves the checked alpha even while viewing a different detail alpha', async () => {
    useAlphaStore.getState().setResult(setupResult());
    useAlphaStore.getState().exitDetailView();
    useAlphaStore.getState().toggleAlphaSelection(0);
    useAlphaStore.getState().enterDetailView(1);
    useAlphaStore.getState().setIsSaveModalOpen(true);
    render(<SaveAlphaModal />);
    fireEvent.change(screen.getByLabelText('Alpha Name'), { target: { value: 'visible_detail' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(saved.size).toBe(1));
    expect(saved.get('visible_detail')).toBe(xml);
    expect(saved.has('visible_detail_alpha_7')).toBe(false);
  });

  it('requires explicit selection even when a detail alpha is already visible', () => {
    useAlphaStore.getState().setResult(setupResult());
    useAlphaStore.getState().setIsSaveModalOpen(true);
    render(<SaveAlphaModal />);
    fireEvent.change(screen.getByLabelText('Alpha Name'), { target: { value: 'unselected' } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('saves all batch-selected alphas while viewing an individual detail', async () => {
    useAlphaStore.getState().setResult(setupResult());
    useAlphaStore.getState().toggleAllAlphaSelection();
    useAlphaStore.getState().enterDetailView(1);
    useAlphaStore.getState().setIsSaveModalOpen(true);
    render(<SaveAlphaModal />);
    fireEvent.change(screen.getByLabelText('Alpha Name'), { target: { value: 'batch' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(saved.size).toBe(2));
    expect(saved.get('batch_alpha_7')).toBe(xml);
    expect(saved.get('batch_alpha_3')).toBe('<data expr="-close" delay="2"/>');
    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([]);
  });

  it('rejects a selection with missing XML before making any writes', async () => {
    useAlphaStore.getState().setResult(setupResult(true));
    useAlphaStore.getState().exitDetailView();
    useAlphaStore.getState().toggleAlphaSelection(0);
    useAlphaStore.getState().toggleAlphaSelection(1);
    useAlphaStore.getState().setIsSaveModalOpen(true);
    render(<SaveAlphaModal />);
    fireEvent.change(screen.getByLabelText('Alpha Name'), { target: { value: 'desk' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('no XML');
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('uses delay zero only for a new editor and preserves loaded or edited XML', () => {
    expect(initial.xmlCode).toBe('<data expr="close" delay="0"/>');
    useAlphaStore.getState().setXmlCode(xml);
    useAlphaStore.getState().setResult(setupResult());
    useAlphaStore.getState().exitDetailView();
    expect(useAlphaStore.getState().xmlCode).toBe(xml);
  });
});
