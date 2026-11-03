import React from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import AlphaRankingTable from '@/components/ResultsTable';
import { useRunStore } from '@/runStore';
import { candidateFixture, multiAlphaMetricFixture } from './metricFixtures';
const initial = useRunStore.getState();
beforeEach(() => useRunStore.setState(initial, true));

describe('result table', () => {
  it('formats fractional metrics and distinguishes average/minimum position counts', () => {
    useRunStore.setState({ result: multiAlphaMetricFixture });
    render(<AlphaRankingTable />);
    expect(screen.getByRole('table', { name: 'Alpha results' })).toBeInTheDocument();
    const row = screen.getByRole('button', { name: 'View alpha_0' }),
      cells = within(row).getAllByRole('cell');
    expect(cells[2]).toHaveTextContent('3.50');
    expect(cells[5]).toHaveTextContent(/^2$/);
    expect(cells[11]).toHaveTextContent('5.25');
    expect(cells[14]).toHaveTextContent(/^1$/);
    expect(cells[3]).toHaveTextContent('2.88%');
    expect(cells[4]).toHaveTextContent('60.00%');
    expect(cells[10]).toHaveTextContent('26.70%');
    expect(within(row).getByText('6.00')).toBeInTheDocument();
    expect(screen.getAllByRole('columnheader', { name: 'sharpe400' })).toHaveLength(2);
    expect(screen.queryByRole('columnheader', { name: 'sharpe80' })).toBeNull();
    expect(cells).toHaveLength(21);
  });
  it('uses sign underlines without coloring turnover or changing metric values', () => {
    useRunStore.setState({ result: multiAlphaMetricFixture });
    render(<AlphaRankingTable />);
    const cells = within(screen.getByRole('button', { name: 'View alpha_0' })).getAllByRole('cell');
    expect(cells[7].firstElementChild).toHaveClass('decoration-metric-positive', 'underline');
    expect(cells[16].firstElementChild).toHaveClass('decoration-metric-negative');
    expect(cells[10].querySelector('.underline')).toBeNull();
    expect(screen.getByRole('columnheader', { name: 'IS' })).toHaveAttribute('colspan', '9');
    expect(screen.getByRole('columnheader', { name: 'OS' })).toHaveAttribute('colspan', '9');
  });
  it('keeps save selection independent from detail activation and supports keyboard selection', () => {
    useRunStore.setState({ result: multiAlphaMetricFixture });
    render(<AlphaRankingTable />);
    const row = screen.getByRole('button', { name: 'View alpha_0' }),
      select = within(row).getByRole('button', { name: 'Select alpha' });
    fireEvent.click(select);
    expect(useRunStore.getState().selectedAlphaIndices).toEqual([0]);
    expect(useRunStore.getState().selectedAlphaIndex).toBeNull();
    fireEvent.keyDown(select, { key: 'Enter' });
    expect(useRunStore.getState().selectedAlphaIndex).toBeNull();
    fireEvent.keyDown(row, { key: 'Enter' });
    expect(useRunStore.getState().selectedAlphaIndex).toBe(0);
    expect(select).toHaveAttribute('aria-pressed', 'true');
  });
  it('sorts displayed rows without changing stable IDs used by saving', () => {
    useRunStore.getState().beginRun(multiAlphaMetricFixture.context, '<source/>');
    useRunStore.getState().receiveSearchEvents([
      {
        type: 'candidate',
        candidate: {
          ...candidateFixture(8),
          summary: {
            is: { ...candidateFixture(8).summary!.is, sharpe: 1 },
            os: candidateFixture(8).summary!.os,
          },
        },
      },
      {
        type: 'candidate',
        candidate: {
          ...candidateFixture(2),
          summary: {
            is: { ...candidateFixture(2).summary!.is, sharpe: 7 },
            os: candidateFixture(2).summary!.os,
          },
        },
      },
    ]);
    render(<AlphaRankingTable />);
    const rows = screen
      .getByRole('table', { name: 'Alpha results' })
      .querySelectorAll('tbody > tr[role="button"]');
    expect(rows[0]).toHaveAttribute('aria-label', 'View alpha_2');
    expect(within(rows[0] as HTMLElement).getAllByRole('cell')[0]).toHaveTextContent('0');
    fireEvent.click(rows[0]);
    expect(useRunStore.getState().selectedAlphaIndex).toBe(1);
    expect(useRunStore.getState().result?.alphas[1].id).toBe(2);
  });
  it('renders failed candidates without fake metric placeholders and shows pool correlation', () => {
    const alpha = { ...candidateFixture(0), poolCorrelation: { ew: 0.125, days: 100 } };
    useRunStore.setState({
      result: { ...multiAlphaMetricFixture, alphas: [alpha, candidateFixture(3, 1, 'bad XML')] },
    });
    render(<AlphaRankingTable />);
    expect(screen.getByRole('button', { name: 'View alpha_3' })).toHaveTextContent(
      'Failed: bad XML',
    );
    expect(screen.getByLabelText('Pool correlation')).toHaveTextContent('0.13');
  });
});
