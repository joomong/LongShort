'use client';

import { useRef, useEffect, useCallback, useState, useMemo, memo } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { useThemeStore } from '@/store/themeStore';
import { getThemeColors } from '@/lib/themeColors';
import { signalToColor } from '@/lib/colorScale';

const MIN_CELL_WIDTH = 1;
const MIN_CELL_HEIGHT = 2;

// Hoisted static empty-state JSX (rendering-hoist-jsx)
const EMPTY_HEATMAP = (
  <div
    className="w-full border-b border-border bg-chart-background"
    style={{ height: '128px' }}
  />
);

function SignalHeatmap() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const searchContainerRef = useRef<HTMLDivElement>(null);
  // rerender-use-ref-transient-values: tooltip updated every mousemove — use ref + imperative DOM
  const tooltipElemRef = useRef<HTMLDivElement>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  // Store computed cell sizes so mouse handlers can use them
  const cellSizeRef = useRef({ w: MIN_CELL_WIDTH, h: MIN_CELL_HEIGHT });
  // Cache canvas bounding rect — updated in drawBase (on resize/data change), avoids getBoundingClientRect() on every mousemove (js-cache-property)
  const canvasRectRef = useRef<DOMRect | null>(null);
  // Granular selectors — avoid subscribing to the full result object for selection state
  const heatmap = useAlphaStore(s => s.result?.heatmap);
  const selectedSymbol = useAlphaStore(s => s.selectedSymbol);
  const selectedTimestamp = useAlphaStore(s => s.selectedTimestamp);
  const setSelectedCell = useAlphaStore(s => s.setSelectedCell);
  const { theme } = useThemeStore();

  // Refs to avoid stale closures in stable draw callback
  const heatmapRef = useRef(heatmap);
  heatmapRef.current = heatmap;
  const selectedSymbolRef = useRef(selectedSymbol);
  selectedSymbolRef.current = selectedSymbol;
  const selectedTimestampRef = useRef(selectedTimestamp);
  selectedTimestampRef.current = selectedTimestamp;
  const themeRef = useRef(theme);
  themeRef.current = theme;
  // Cached base heatmap pixels — restored on selection change to avoid full redraw
  const baseImageDataRef = useRef<ImageData | null>(null);
  // Cached theme colors — populated lazily in drawBase, avoids getThemeColors() on every render (rerender-lazy-state-init)
  const themeColorsRef = useRef<ReturnType<typeof getThemeColors> | null>(null);

  // Build index Maps for O(1) lookup (js-index-maps)
  const symbolMap = useMemo(() => {
    if (!heatmap) return new Map<string, number>();
    return new Map(heatmap.symbols.map((s, i) => [s, i]));
  }, [heatmap]);

  const timeMap = useMemo(() => {
    if (!heatmap) return new Map<string, number>();
    return new Map(heatmap.timestamps.map((t, i) => [t, i]));
  }, [heatmap]);

  const symbolMapRef = useRef(symbolMap);
  symbolMapRef.current = symbolMap;
  const timeMapRef = useRef(timeMap);
  timeMapRef.current = timeMap;

  // Compute filtered symbols for search — 17 symbols max, filter cost < useMemo overhead (rerender-simple-expression-in-memo)
  const filteredSymbols = (!searchQuery || !heatmap)
    ? []
    : heatmap.symbols.filter(s => s.toLowerCase().includes(searchQuery.toLowerCase()));

  // Click outside handler for search dropdown
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (searchContainerRef.current && !searchContainerRef.current.contains(e.target as Node)) {
        setIsSearchOpen(false);
      }
    };
    if (isSearchOpen) {
      document.addEventListener('mousedown', handleClickOutside, { passive: true });
      return () => document.removeEventListener('mousedown', handleClickOutside);
    }
  }, [isSearchOpen]);

   // Overlay only: restore cached pixels then draw highlight. O(1) per click.
   // Declared before drawBase so drawBase can call it directly.
   // eslint-disable-next-line @typescript-eslint/no-unused-vars
const drawSelectionOverlay = useCallback((ctx: CanvasRenderingContext2D, canvas: HTMLCanvasElement) => {
     const currentSelectedSymbol = selectedSymbolRef.current;
     const currentSelectedTimestamp = selectedTimestampRef.current;
     const currentSymbolMap = symbolMapRef.current;
     const currentTimeMap = timeMapRef.current;
     const { w: cellW, h: cellH } = cellSizeRef.current;

     if (baseImageDataRef.current) {
       ctx.putImageData(baseImageDataRef.current, 0, 0);
     }

     if (currentSelectedSymbol && currentSelectedTimestamp) {
       const symbolIdx = currentSymbolMap.get(currentSelectedSymbol);
       const timeIdx = currentTimeMap.get(currentSelectedTimestamp);
       if (symbolIdx !== undefined && timeIdx !== undefined) {
         ctx.strokeStyle = themeColorsRef.current?.accent ?? '#0078ff';
         ctx.lineWidth = 2;
         ctx.strokeRect(Math.floor(timeIdx * cellW), Math.floor(symbolIdx * cellH), Math.ceil(cellW), Math.ceil(cellH));
       }
     }
   }, []); // Stable identity: reads latest values from refs, not closures

  // Full redraw: all cells + cache ImageData. Called only when heatmap data or theme changes.
  const drawBase = useCallback(() => {
     const canvas = canvasRef.current;
     const container = containerRef.current;
     const currentHeatmap = heatmapRef.current;
     const currentTheme = themeRef.current;

     if (!canvas || !container || !currentHeatmap) return;

     const ctx = canvas.getContext('2d');
     if (!ctx) return;

     void currentTheme; // Trigger redraw on theme change (getThemeColors reads CSS variables)
     const colors = getThemeColors();
     themeColorsRef.current = colors; // Cache for drawSelectionOverlay (avoids getComputedStyle per click)
     const { timestamps, symbols, values } = currentHeatmap;
     const numTimestamps = timestamps.length;
     const numSymbols = symbols.length;
     if (numTimestamps === 0 || numSymbols === 0) return;

     const containerWidth = container.clientWidth;
     const containerHeight = container.clientHeight;

     const cellW = Math.max(MIN_CELL_WIDTH, containerWidth / numTimestamps);
     const cellH = Math.max(MIN_CELL_HEIGHT, containerHeight / numSymbols);
     cellSizeRef.current = { w: cellW, h: cellH };

     const width = Math.ceil(cellW * numTimestamps);
     const height = Math.ceil(cellH * numSymbols);

     canvas.width = width;
     canvas.height = height;
     canvas.style.width = `${containerWidth}px`;
     canvas.style.height = `${height}px`;
     // Invalidate cached rect — getCell will lazily re-read on first mousemove after resize/data change (js-cache-property)
     canvasRectRef.current = null;

     ctx.fillStyle = colors.chartBackground;
     ctx.fillRect(0, 0, width, height);

     // Precompute rank-based color palettes per unique sortedLen (js-cache-function-results)
     // signalToColor() constructs rgb() strings — cache to avoid ~6k calls per draw
     const NAN_COLOR = signalToColor(NaN, -1, 1);
     const ZERO_COLOR = signalToColor(0, -1, 1);
     const colorPalettes = new Map<number, string[]>();

     // Per-timestamp rank-based normalization
     const colSorted: number[][] = new Array(numTimestamps);
     for (let t = 0; t < numTimestamps; t++) {
       const col = values[t];
       const finite: number[] = [];
       if (col) {
         for (let s = 0; s < numSymbols; s++) {
           const v = col[s];
           if (typeof v === 'number' && isFinite(v)) finite.push(v);
         }
       }
       colSorted[t] = finite.toSorted((a, b) => a - b);

       // Build palette for this sortedLen if not already cached
       const sortedLen = colSorted[t].length;
       if (sortedLen > 1 && !colorPalettes.has(sortedLen)) {
         const palette = new Array<string>(sortedLen);
         for (let r = 0; r < sortedLen; r++) {
           palette[r] = signalToColor((r / (sortedLen - 1)) * 2 - 1, -1, 1);
         }
         colorPalettes.set(sortedLen, palette);
       }
     }

     for (let timeIdx = 0; timeIdx < numTimestamps; timeIdx++) {
       const col = values[timeIdx]; // js-cache-property: cache per-timestamp column
       for (let symbolIdx = 0; symbolIdx < numSymbols; symbolIdx++) {
         const value = col?.[symbolIdx];
         let color: string;
         if (typeof value !== 'number' || !isFinite(value)) {
           color = NAN_COLOR;
         } else {
           const sorted = colSorted[timeIdx];
           const sortedLen = sorted.length;
           if (sortedLen <= 1) {
             color = ZERO_COLOR;
           } else {
             let lo = 0, hi = sortedLen;
             while (lo < hi) {
               const mid = (lo + hi) >> 1;
               if (sorted[mid] < value) lo = mid + 1;
               else hi = mid;
             }
             color = colorPalettes.get(sortedLen)![lo];
           }
         }

         ctx.fillStyle = color;
         ctx.fillRect(Math.floor(timeIdx * cellW), Math.floor(symbolIdx * cellH), Math.ceil(cellW), Math.ceil(cellH));
       }
     }

     // Cache pixels after drawing base — selection overlay restores from this
     baseImageDataRef.current = ctx.getImageData(0, 0, canvas.width, canvas.height);

     // Draw initial selection if any
     drawSelectionOverlay(ctx, canvas);
  }, []); // Stable identity: reads latest values from refs, not closures

  const drawSelection = useCallback(() => {
     const canvas = canvasRef.current;
     if (!canvas) return;
     const ctx = canvas.getContext('2d');
     if (!ctx) return;
     drawSelectionOverlay(ctx, canvas);
  }, []); // Stable identity: reads latest values from refs, not closures

   // Full redraw only when heatmap data or theme changes
   useEffect(() => {
     drawBase();
   }, [drawBase, heatmap, theme]);

   // Fast overlay-only redraw when selection changes
   useEffect(() => {
     drawSelection();
   }, [drawSelection, selectedSymbol, selectedTimestamp]);

  // Redraw on container resize
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(() => drawBase());
    observer.observe(container);
    return () => observer.disconnect();
  }, [drawBase]);

  const getCell = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    const currentHeatmap = heatmapRef.current;
    if (!canvas || !currentHeatmap) return null;

    // Use cached rect — lazily populated on first mousemove after drawBase invalidates it (js-cache-property)
    const rect = canvasRectRef.current ?? (canvasRectRef.current = canvas.getBoundingClientRect());
    // Account for CSS scaling (canvas pixel size vs display size)
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    const x = (e.clientX - rect.left) * scaleX;
    const y = (e.clientY - rect.top) * scaleY;

    const { w, h } = cellSizeRef.current;
    const timeIdx = Math.floor(x / w);
    const symbolIdx = Math.floor(y / h);

    if (timeIdx >= 0 && timeIdx < currentHeatmap.timestamps.length &&
        symbolIdx >= 0 && symbolIdx < currentHeatmap.symbols.length) {
      return { timeIdx, symbolIdx };
    }
    return null;
  }, []); // Stable identity: reads latest heatmap via ref, not closure

  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = getCell(e);
    const el = tooltipElemRef.current;
    const currentHeatmap = heatmapRef.current;
    if (!cell || !currentHeatmap) {
      if (el) el.style.display = 'none';
      return;
    }

    if (el) {
      const rawValue = currentHeatmap.values[cell.timeIdx]?.[cell.symbolIdx];
      const value = typeof rawValue === 'number' && isFinite(rawValue) ? rawValue : null;
      // Update text content directly — no React re-render on every pixel (rerender-use-ref-transient-values)
      const children = el.children as HTMLCollectionOf<HTMLElement>;
      children[0].textContent = currentHeatmap.symbols[cell.symbolIdx];
      children[1].textContent = currentHeatmap.timestamps[cell.timeIdx];
      children[2].textContent = value === null ? 'N/A' : value.toFixed(4);
      children[2].className = `font-mono ${
        value === null ? 'text-foreground-muted' :
        value > 0 ? 'text-metric-negative' :
        value < 0 ? 'text-accent' : 'text-foreground-muted'
      }`;
      el.style.left = `${e.clientX + 10}px`;
      el.style.top = `${e.clientY + 10}px`;
      // flip computed in handler (window always defined in event callbacks)
      el.style.transform = e.clientX > window.innerWidth * 0.6
        ? 'translateX(calc(-100% - 20px))' : '';
      el.style.display = 'block';
    }
  }, []); // Stable identity: reads latest heatmap via ref, not closure

  const handleMouseLeave = useCallback(() => {
    const el = tooltipElemRef.current;
    if (el) el.style.display = 'none';
  }, []);

  // searchQueryRef keeps handleSearchFocus identity stable (advanced-use-latest)
  const searchQueryRef = useRef(searchQuery);
  searchQueryRef.current = searchQuery;

  const handleSearchChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setSearchQuery(e.target.value);
    setIsSearchOpen(e.target.value.length > 0);
  }, []);

  const handleSearchFocus = useCallback(() => {
    setIsSearchOpen(searchQueryRef.current.length > 0);
  }, []); // stable identity: reads latest searchQuery via ref

  // Stable handler for symbol dropdown items — reads symbol from data attribute (rerender-memo)
  const handleSymbolClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const symbol = e.currentTarget.dataset.symbol;
    if (symbol) {
      setSelectedCell(symbol, null);
      setSearchQuery('');
      setIsSearchOpen(false);
    }
  }, [setSelectedCell]);

  const handleClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = getCell(e);
    const currentHeatmap = heatmapRef.current;
    if (!cell || !currentHeatmap) return;

    const symbol = currentHeatmap.symbols[cell.symbolIdx];
    const timestamp = currentHeatmap.timestamps[cell.timeIdx];

    if (symbol && timestamp) {
      // Use refs for toggle check — stable callback, no stale-closure risk (advanced-use-latest)
      if (symbol === selectedSymbolRef.current && timestamp === selectedTimestampRef.current) {
        setSelectedCell(null, null);
      } else {
        setSelectedCell(symbol, timestamp);
      }
    }
  }, [setSelectedCell]); // Stable identity: reads latest heatmap via ref, not closure

  if (!heatmap) {
    return EMPTY_HEATMAP;
  }

  return (
    <div ref={containerRef} className="min-h-[120px] max-h-48 overflow-y-auto bg-background border-b border-border relative" onScroll={() => { canvasRectRef.current = null; }}>
      {/* Symbol search box */}
      <div ref={searchContainerRef} className="absolute top-2 left-2 z-10">
        <input
          type="text"
          value={searchQuery}
          onChange={handleSearchChange}
          onFocus={handleSearchFocus}
          placeholder="Search symbol"
          className="w-28 px-2 py-1 text-xs font-mono bg-background-elevated border border-border text-data-color focus:outline-none focus:border-accent"
        />
        {isSearchOpen && filteredSymbols.length > 0 ? (
          <div className="absolute top-full left-0 mt-1 w-28 max-h-48 overflow-y-auto bg-background-elevated border border-border shadow-elevated">
            {filteredSymbols.map((symbol) => (
              <div
                key={symbol}
                data-symbol={symbol}
                className="px-2 py-1 text-xs font-mono text-data-color cursor-pointer hover:bg-row-active"
                onClick={handleSymbolClick}
              >
                {symbol}
              </div>
            ))}
          </div>
        ) : null}
      </div>

      <canvas
        ref={canvasRef}
        onClick={handleClick}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        className="cursor-pointer block"
      />
      {/* Persistent tooltip — content/position updated imperatively in handleMouseMove (rerender-use-ref-transient-values) */}
      {/* Signal context: positive = short bias (bearish) → red; negative = long bias (bullish) → blue */}
      <div
        ref={tooltipElemRef}
        className="fixed z-50 px-3 py-2 text-xs bg-background-elevated border border-border-strong shadow-elevated pointer-events-none"
        style={{ display: 'none' }}
      >
        <div className="font-medium text-foreground" />
        <div className="text-foreground-secondary" />
        <div className="font-mono" />
      </div>
    </div>
  );
}

export default memo(SignalHeatmap);
