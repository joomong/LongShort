'use client';

import { useState, useRef, useCallback, useEffect, ReactNode } from 'react';

interface MainLayoutProps {
  topBar?: ReactNode;
  leftPanel: ReactNode;
  rightPanel: ReactNode;
  defaultLeftWidth?: number; // percentage
  minLeftWidth?: number;
  maxLeftWidth?: number;
}

export default function MainLayout({
  topBar,
  leftPanel,
  rightPanel,
  defaultLeftWidth = 24,
  minLeftWidth = 20,
  maxLeftWidth = 80,
}: MainLayoutProps) {
  const [leftWidth, setLeftWidth] = useState(defaultLeftWidth);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef(0);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  useEffect(() => {
    if (!isDragging) return;

    const handleMouseMove = (e: MouseEvent) => {
      if (rafRef.current) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = 0;
        if (!containerRef.current) return;
        const rect = containerRef.current.getBoundingClientRect();
        const newWidth = ((e.clientX - rect.left) / rect.width) * 100;
        const clampedWidth = Math.min(Math.max(newWidth, minLeftWidth), maxLeftWidth);
        setLeftWidth(clampedWidth);
      });
    };

    const handleMouseUp = () => {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
      setIsDragging(false);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isDragging, minLeftWidth, maxLeftWidth]);

  return (
    <div className="flex flex-col h-dvh w-full overflow-hidden bg-background">
      {/* Top Bar - fixed above the split */}
      {topBar && (
        <div className="flex-shrink-0 w-full">
          {topBar}
        </div>
      )}

      {/* Split panels below */}
      <div ref={containerRef} className="flex flex-1 min-h-0 w-full overflow-hidden">
        {/* Left Panel - Editor */}
        <div
          className="h-full min-w-0 shrink-0 overflow-hidden"
          style={{ width: `${leftWidth}%` }}
        >
          {leftPanel}
        </div>

        {/* Resizer */}
        <div
          className={`w-[4px] shrink-0 h-full cursor-col-resize transition-colors focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none ${
            isDragging ? 'bg-accent' : 'bg-border-strong hover:bg-accent/50'
          }`}
          role="separator"
          aria-label="Resize editor and results panels"
          aria-orientation="vertical"
          aria-valuemin={minLeftWidth}
          aria-valuemax={maxLeftWidth}
          aria-valuenow={Math.round(leftWidth)}
          tabIndex={0}
          onMouseDown={handleMouseDown}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft') { e.preventDefault(); setLeftWidth(w => Math.max(minLeftWidth, w - 1)); }
            if (e.key === 'ArrowRight') { e.preventDefault(); setLeftWidth(w => Math.min(maxLeftWidth, w + 1)); }
          }}
        />

        {/* Right Panel - Visualization */}
        <div
          className="h-full min-w-0 flex-1 overflow-hidden flex flex-col"
        >
          {rightPanel}
        </div>
      </div>
    </div>
  );
}
