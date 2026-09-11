import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import DetailSummaryTable from '@/components/visualization/DetailSummaryTable';
import { useAlphaStore } from '@/store/alphaStore';
import { multiAlphaFixture } from './pipelineFixtures';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  useAlphaStore.setState(initialState, true);
});

describe('DetailSummaryTable render output', () => {
  it('renders yearly columns from C++ yearly data', () => {
    render(
      <DetailSummaryTable
        alpha={multiAlphaFixture.alphas[0]}
      />
    );

    expect(screen.getByText('Yearly Table')).toBeInTheDocument();
    const row = screen.getByText('2025').closest('tr')!;
    // Actual C++ fixture: returns/DD are percentage points, exposure/turnover fractions.
    expect(within(row).getAllByRole('cell').map(cell => cell.textContent)).toEqual([
      '2025', '50.0%', '50.0%', '-5.1%', '-14.8%', '-7.17', '-5.1%', '8.8', '-0.0067',
    ]);
  });

  it('renders empty state when no yearly data', () => {
    const alphaNoYearly = { ...multiAlphaFixture.alphas[0], yearly: undefined };
    render(
      <DetailSummaryTable alpha={alphaNoYearly} />
    );

    expect(screen.getByText('No yearly summary (missing yearly data from engine)')).toBeInTheDocument();
  });
});
