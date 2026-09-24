'use client';

import { useRef, useEffect, useCallback, useState, useMemo, memo } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { selectSignalSource } from '@/lib/signalSelection';
import { useThemeStore } from '@/store/themeStore';
import { getThemeColors } from '@/lib/themeColors';
import { zscoreToRgb, parseHex } from '@/lib/colorScale';
import { symbolLabel, symbolMatches } from '@/lib/symbolLabels';
import { robustCrossSection } from '@/lib/heatmapStats';
import { SIGNAL_HEATMAP_HEIGHT } from '@/lib/signalPanelLayout';

// The whole panel fits the container (no scrolling): the canvas is the container's
// pixel grid and each pixel shows the cell it lands on.
const MAX_CANVAS_DIM = 8192;

// Hoisted static empty-state JSX (rendering-hoist-jsx)
const EMPTY_HEATMAP = (
  <div
    className="w-full shrink-0 overflow-hidden border-b border-border bg-chart-background"
    style={{ height: SIGNAL_HEATMAP_HEIGHT }}
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
  const cellSizeRef = useRef({ w: 1, h: 1 });
  // Granular selectors — avoid subscribing to the full result object for selection state
  const heatmap = useAlphaStore(s => selectSignalSource(s)?.heatmap);
  const selectedSymbol = useAlphaStore(s => s.selectedSymbol);
  const selectedTimestamp = useAlphaStore(s => s.selectedTimestamp);
  const setSelectedCell = useAlphaStore(s => s.setSelectedCell);
  const symbolLabels = useAlphaStore(s => s.config?.symbolLabels ?? null);
  const { theme } = useThemeStore();
  const symbolLabelsRef = useRef(symbolLabels);
  symbolLabelsRef.current = symbolLabels;

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
    : heatmap.symbols.filter(s => symbolMatches(s, searchQuery, symbolLabels)).slice(0, 200);

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
const drawSelectionOverlay = useCallback((ctx: CanvasRenderingContext2D) => {
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
         ctx.strokeStyle = themeColorsRef.current?.accent ?? '#ee9d24';
         ctx.lineWidth = 1;
         const x = timeIdx * cellW, y = symbolIdx * cellH;
         ctx.strokeRect(Math.floor(x) - 1, Math.floor(y) - 1, Math.ceil(cellW) + 2, Math.ceil(cellH) + 2);
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

     const colors = getThemeColors(currentTheme);
     themeColorsRef.current = colors; // Cache for drawSelectionOverlay (avoids getComputedStyle per click)
     const { timestamps, symbols, values } = currentHeatmap;
     const numTimestamps = timestamps.length;
     const numSymbols = symbols.length;
     if (numTimestamps === 0 || numSymbols === 0) return;

     const containerWidth = container.clientWidth;
     const containerHeight = container.clientHeight;
     if (containerWidth === 0 || containerHeight === 0) return;

     // Pixel grid: one canvas pixel per cell when it fits, otherwise the canvas is
     // downscaled by CSS so every symbol stays on screen (no vertical scrolling).
     const dpr = Math.min(2, window.devicePixelRatio || 1);
     const width = Math.max(1, Math.min(MAX_CANVAS_DIM, Math.round(containerWidth * dpr)));
     const height = Math.max(1, Math.min(MAX_CANVAS_DIM, Math.round(containerHeight * dpr)));
     const cellW = width / numTimestamps;
     const cellH = height / numSymbols;
     cellSizeRef.current = { w: cellW, h: cellH };

     canvas.width = width;
     canvas.height = height;
     canvas.style.width = `${containerWidth}px`;
     canvas.style.height = `${containerHeight}px`;

     // Cross-sectional robust z-score per date: red is above the same-date median, blue below.
     // Dates are independent; raw values remain unchanged for selection and tooltips.
     const base = parseHex(colors.chartBackground) ?? [255, 255, 255];
     const zRows = robustCrossSection(values, numSymbols);

     const image = ctx.createImageData(width, height);
     const px = image.data;
     const rowSymbol = new Int32Array(height);
     for (let y = 0; y < height; y++) rowSymbol[y] = Math.min(numSymbols - 1, Math.floor(y / cellH));
     const colTime = new Int32Array(width);
     for (let x = 0; x < width; x++) colTime[x] = Math.min(numTimestamps - 1, Math.floor(x / cellW));
     for (let y = 0; y < height; y++) {
       const si = rowSymbol[y];
       let o = y * width * 4;
       for (let x = 0; x < width; x++, o += 4) {
         const z = zRows[colTime[x]][si];
         if (Number.isNaN(z)) {
           px[o] = base[0]; px[o + 1] = base[1]; px[o + 2] = base[2];
         } else {
           const [r, g, b] = zscoreToRgb(z, base);
           px[o] = r; px[o + 1] = g; px[o + 2] = b;
         }
         px[o + 3] = 255;
       }
     }
     ctx.putImageData(image, 0, 0);

     // Cache pixels after drawing base — selection overlay restores from this
     baseImageDataRef.current = ctx.getImageData(0, 0, canvas.width, canvas.height);

     // Draw initial selection if any
     drawSelectionOverlay(ctx);
  }, [drawSelectionOverlay]); // Stable identity: reads latest values from refs, not closures

  const drawSelection = useCallback(() => {
     const canvas = canvasRef.current;
     if (!canvas) return;
     const ctx = canvas.getContext('2d');
     if (!ctx) return;
     drawSelectionOverlay(ctx);
  }, [drawSelectionOverlay]); // Stable identity: reads latest values from refs, not closures

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
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(drawBase);
    });
    observer.observe(container);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [drawBase, heatmap]);

  const getCell = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    const currentHeatmap = heatmapRef.current;
    if (!canvas || !currentHeatmap) return null;

    const rect = canvas.getBoundingClientRect();
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
      children[0].textContent = symbolLabel(currentHeatmap.symbols[cell.symbolIdx], symbolLabelsRef.current);
      children[1].textContent = currentHeatmap.timestamps[cell.timeIdx];
      children[2].textContent = value === null ? 'N/A' : value.toFixed(4);
      children[2].className = 'font-mono text-[#222222] dark:text-[#dddddd]';
      el.style.left = `${e.clientX + 10}px`;
      el.style.top = `${e.clientY + 10}px`;
      // flip computed in handler (window always defined in event callbacks)
      el.style.transform = e.clientX > window.innerWidth * 0.6
        ? 'translateX(calc(-100% - 20px))' : '';
      el.style.display = 'block';
    }
  }, [getCell]); // Stable identity: reads latest heatmap via ref, not closure

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
  }, [getCell, setSelectedCell]); // Stable identity: reads latest heatmap via ref, not closure

  if (!heatmap) {
    return EMPTY_HEATMAP;
  }

  return (
    <div ref={containerRef} style={{ height: SIGNAL_HEATMAP_HEIGHT }} className="shrink-0 min-h-0 overflow-hidden bg-background border-b border-border relative" data-testid="heatmap-panel">
      {/* Symbol search box */}
      <div ref={searchContainerRef} className="absolute top-2 left-2 z-10">
        <input
          type="text"
          value={searchQuery}
          onChange={handleSearchChange}
          onFocus={handleSearchFocus}
          placeholder="Search symbol"
          className="w-28 px-2 py-0.5 text-[11px] font-mono bg-background border border-border text-foreground focus:outline-none focus:border-accent"
        />
        {isSearchOpen && filteredSymbols.length > 0 ? (
          <div className="absolute top-full left-0 mt-px w-28 max-h-[136px] overflow-y-auto bg-background border border-border shadow-card">
            {filteredSymbols.map((symbol) => (
              <div
                key={symbol}
                data-symbol={symbol}
                className="px-2 py-0.5 text-[11px] font-mono text-foreground cursor-pointer hover:bg-row-active"
                onClick={handleSymbolClick}
              >
                {symbolLabel(symbol, symbolLabels)}
              </div>
            ))}
          </div>
        ) : null}
      </div>

      <canvas
        aria-label="Signal heatmap"
        ref={canvasRef}
        onClick={handleClick}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        className="cursor-pointer block"
      />
      {/* Persistent tooltip — content/position updated imperatively in handleMouseMove (rerender-use-ref-transient-values) */}
      {/* Neutral tooltip colors keep values readable over the heatmap. */}
      <div
        ref={tooltipElemRef}
        role="tooltip"
        className="fixed z-50 px-2 py-1 text-[11px] text-[#222222] dark:text-[#dddddd] bg-[#f7f7f7] dark:bg-[#252525] border border-[#bdbdbd] dark:border-[#555555] shadow-sm pointer-events-none"
        style={{ display: 'none' }}
      >
        <div className="font-medium" />
        <div className="text-[#555555] dark:text-[#bbbbbb]" />
        <div className="font-mono" />
      </div>
    </div>
  );
}

export default memo(SignalHeatmap);
