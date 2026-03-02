import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

import SummaryMetrics from '@/components/multi-alpha/SummaryMetrics';
import { useAlphaStore } from '@/store/alphaStore';
import { multiAlphaFixture } from './pipelineFixtures';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  useAlphaStore.setState(initialState, true);
});

describe('SummaryMetrics render formatting', () => {
  it('renders combined IS/OS values with exact display format', () => {
    useAlphaStore.setState({ result: multiAlphaFixture });

    render(<SummaryMetrics />);

    expect(screen.getByText('Combined Portfolio')).toBeInTheDocument();
    // IS: Return, Sharpe, Turnover
    expect(screen.getByText('+947.5%')).toBeInTheDocument();
    expect(screen.getByText('5.06')).toBeInTheDocument();
    expect(screen.getByText('26.7')).toBeInTheDocument();

    // OS: Return, Sharpe
    expect(screen.getByText('-60.3%')).toBeInTheDocument();
    expect(screen.getByText('-4.50')).toBeInTheDocument();
  });
});
