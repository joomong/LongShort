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
    expect(screen.getByText('-4.9%')).toBeInTheDocument();
    expect(screen.getByText('-4.65')).toBeInTheDocument();
    expect(screen.getByText('9.2%')).toBeInTheDocument();

    // OS: Return, Sharpe
    expect(screen.getByText('-28.0%')).toBeInTheDocument();
    expect(screen.getByText('-11.05')).toBeInTheDocument();
  });
});
