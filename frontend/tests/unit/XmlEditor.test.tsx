import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import XmlEditor from '@/components/XmlEditor';
import { useRunStore } from '@/runStore';
import { multiAlphaMetricFixture } from './metricFixtures';

// Model Monaco's controlled value/change/readOnly contract without its CDN runtime.
vi.mock('next/dynamic', () => ({
  default: () =>
    function MockMonaco({
      value,
      onChange,
      options,
    }: {
      value: string;
      onChange: (value: string) => void;
      options: { readOnly: boolean };
    }) {
      return (
        <textarea
          aria-label="XML source"
          value={value}
          readOnly={options.readOnly}
          onChange={(event) => onChange(event.target.value)}
        />
      );
    },
}));

const initialState = useRunStore.getState();
const originalXml = '<choice><data expr="close"/><data expr="volume"/></choice>';
const selectedXml = '<data expr="close"/>';

beforeEach(() => {
  useRunStore.setState(initialState, true);
  useRunStore.getState().setXmlCode(originalXml);
  useRunStore.setState({
    result: {
      ...multiAlphaMetricFixture,
      alphas: multiAlphaMetricFixture.alphas.map((alpha, index) => ({
        ...alpha,
        xml: index === 0 ? selectedXml : '<data expr="volume"/>',
      })),
    },
  });
  useRunStore.getState().exitDetailView();
});
afterEach(() => act(() => useRunStore.setState(initialState, true)));

describe('original XML editor while viewing alpha details', () => {
  it('retains the editable original when selecting and switching alpha details', () => {
    render(<XmlEditor />);
    const editor = screen.getByRole('textbox', { name: 'XML source' });
    expect(editor).toHaveValue(originalXml);

    act(() => useRunStore.getState().enterDetailView(0));
    expect(useRunStore.getState().viewMode).toBe('detail');
    expect(editor).toHaveValue(originalXml);
    expect(editor).not.toHaveAttribute('readonly');

    act(() => useRunStore.getState().enterDetailView(1));
    expect(editor).toHaveValue(originalXml);
    expect(editor).not.toHaveAttribute('readonly');
  });

  it('edits only the original XML and retains those edits while multi-alpha detail stays open', () => {
    useRunStore.getState().enterDetailView(0);
    render(<XmlEditor />);
    const editor = screen.getByRole('textbox', { name: 'XML source' });
    const editedXml = '<choice><data expr="returns"/></choice>';

    expect(editor).not.toHaveAttribute('readonly');
    fireEvent.change(editor, { target: { value: editedXml } });
    expect(useRunStore.getState().xmlCode).toBe(editedXml);
    expect(useRunStore.getState().result?.alphas[0].xml).toBe(selectedXml);
    expect(useRunStore.getState().result?.alphas[1].xml).toBe('<data expr="volume"/>');
    expect(editor).toHaveValue(editedXml);

    act(() => useRunStore.getState().exitDetailView());
    expect(useRunStore.getState().viewMode).toBe('detail');
    expect(editor).toHaveValue(editedXml);
  });
});
